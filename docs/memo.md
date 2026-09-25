# Working Memo

Running record of benchmark work: what was run, what the numbers were, what was
decided, and why. Newest entries go at the top. Append to this file as work
continues; do not rewrite past entries when a later finding contradicts them —
add the correction and say what it overturns.

---

## 2026-09-25 — `analyze_we` の収束判定: 「その時点で止めていたら報告された速度」の後半での動き（metadynamics の dF(t) に対応）

ユーザー要望（metadynamics と同じようにサンプリングの収束を判定する図、WE に合った手法で）。`analyze_metadynamics` の設計（1 つの数値 = 後半での dF(t) の範囲、1 枚の図 = dF(t)、後半を緑 / 赤で塗る、許容 1 kT）に合わせた:

- 手法: `we/kinetics.py::rate_history` が、各 round で run を止めていたら `analyze_we` が報告した速度（`steady_state_rate` を先頭からその round までに適用、窓の選び方も込み）を並べ、`history_drift` がその後半での範囲 `ln(max k / min k)` を取る。`k ∝ exp(−ΔG‡/kT)` なのでこれは障壁の揺れを kT で測ったもので、許容は metadynamics と同じ 1 kT（速度で e 倍）。後半の開始時点でまだ到達が無ければ無限大（後半に初めて出た速度は保証されない）。後半の全 round を評価し、前半は図用に 40 点へ間引く。
- 判定: 窓が定常（従来の `flux_steady` 条件）でも、drift が 1 kT 以上なら新しい verdict `rate_not_converged`（`next_rounds_suggested` = run の半分、10–50）。複数 scheme では node の verdict を最も未収束の scheme のものにし（`verdict_scheme_id`、`next` は `next_scheme_id` を延長）、scheme 間で速度が e 倍以上違えば `schemes_disagree` 警告。
- 図 `we_convergence.png`（artifact `we_convergence_plot`、scheme ごとに 1 パネル）と `we_convergence.csv`（`we_convergence`）。`--drift-tolerance-kt`（既定 1.0）。
- 却下した案: 最終窓の中だけで前向き / 後ろ向き累積平均を見る方法。窓は定常になるよう選ばれているので、中だけ見ても動きが出ない。unf1 を 40 round で止めた場合、この方法だと ×1.46 で「収束」になるが、実際の推定値は最終値の約 3 倍だった。
- キャンペーン実データで較正（ledger を読むだけ、DAG は変更なし）: we1 ×2.51（0.92 kT）収束、we2 ×6.4（1.86 kT、26 ns 付近のバースト）未収束、unf1 ×2.06（0.72 kT）収束（途中の 30–90 round で止めた場合はすべて未収束 = 初到達直後の行き過ぎを検出）、fold1 ×7.0（1.95 kT、60 round 以降に推定値が約 3 倍へ跳ねた）未収束。範囲でなく最終値からの距離にしても判定は同じ（we1 ×2.19、we2 ×2.97、unf1 ×1.99、fold1 ×4.30）。
- 限界: 閾値内で単調に上がり続ける場合は収束と判定される（we1 は後半で 1.2e7 → 3e7 と上昇）。skill には「上がり続けているならそう書く」と明記した。
- 同時に既存バグを修正: 定常区間が 8 round 未満のとき、判定理由の組み立てが `level_check` の `head_mean` を読んで `KeyError` になっていた（32d4b51 に含まれていた。短い run の履歴計算で発覚）。
- テスト: `test_we_kinetics.py::TestConvergence`（5 件: 履歴の端、定常な長い run は収束、後半で 4 倍に跳ぶと `rate_not_converged`、中間より後の初到達、`history_drift` の境界）。WE-23b のバースト試験は、窓の選び方はそのまま検証しつつ verdict を `flux_steady` / `rate_not_converged` のどちらでもよいとした（8 倍のバーストは実際に推定値を約 1 kT 動かす）。`test_we_nodes` に収束 artifact の検証を追加。

## 2026-09-21 — `run_rounds --executor mps`: round の segment を `submit_mps_job` の task として投げる（テスター WE-4）

- 実装: `mdclaw/rounds/driver.py`。`--executor mps` では pending segment を `mps_tasks_per_gpu × mps_gpus` 本ずつ `submit_mps_job` に渡し（task = `mdclaw --job-dir .. --node-id .. run_production <stage_args をフラグ化> --random-seed .. --platform CUDA`）、`check_job` を `mps_poll_seconds` 間隔で回して待つ。policy（`we_resample`）はドライバの python に OpenMM が無ければ launcher 経由（コンテナ内）、あれば同一プロセス。ドライバ自体はホストで動く（`bin/mdclaw` が `run_rounds --executor mps` を native に回す）。Slurm のスクリプト・ログは `<job_dir>/slurm/`。
- 失敗の扱い: job が終わったのに seal されていない segment は `check_job` の sync が zombie failed にし（`slurm_completed_without_node_completion`）、`slurm_job_vanished` の stranded ノードはドライバが failed にする → いずれも通常の retry（`_t000N`、3 回で `rounds_replica_unstable`）。前のドライバが queued/running のまま残した segment は再投入せず待つ（local executor は従来どおり `rounds_round_in_progress`）。新 code: `rounds_submit_failed`、`rounds_slurm_unavailable`（20 回連続で queue が読めない）。
- テスト: `tests/test_rounds_mps.py` 7 件。fake submit がノードを queued に stamp し、fake `check_job` が task コマンドを本物の CLI サブプロセスで実行（CUDA→Reference 置換）してから本物の sync を当てるので、コマンド文字列・zombie・vanished・resume・submit 拒否・policy の launcher 経路まで通る。SIF で 7 passed（20 s）、rounds/WE/契約/registry/envelope/node の 336 passed、ruff clean。golden 再生成（462 codes、95 tools）。
- 同時に WE-6（軽微）: `production_preflight` は prod 以外のノード（eq の submit）や別ツールの literal コマンドを `not_applicable` にして警告しない（opaque なスクリプトの prod ノードだけ `skipped` 警告）。`setup_rounds` / `run_rounds` / `inspect_rounds` の結果に `message` と scheme 単位の `next`（`scheme_next`: run / wait / done）を付け、envelope の `message: "ok"`・`next: null` を解消。
- WE-16（テスターが we1 の driver job を scancel して発見: running のまま持ち主の消えた segment を `inspect_rounds` が永遠に wait させる）: `mdclaw/rounds/owner.py` を追加。local executor と in-process の policy は node を走らせる前に `nodes/<id>/owner.json`（host, pid, driver の SLURM_JOB_ID, heartbeat）を書き、30 s ごとに heartbeat を更新、終了時に削除。持ち主の消えた running（同 host は pid 死亡で即、別 host は heartbeat 5 分超）は stale: `run_rounds` が `rounds_owner_lost` で failed にして retry（結果 `recovered`）、`inspect_rounds` は `stale` を返して next を run_rounds に。生きた持ち主は `rounds_round_in_progress` の message に host/pid/job/heartbeat 年齢と手動解放（`update_workflow_state --clear-slurm-metadata`、running かつ job 無しでも pending に戻る）を示す。owner record の無い running（手動・旧 driver・mps task）は判定しない。テスト 6 件（`TestOwnerRecovery`）。
- WE-17（提案: MPS task 1 本で k segment を順に回して起動 ~11 s/segment を償却; B の 0.2 ns で GPU 時間の ~3 割）: 妥当だが `submit_mps_job` の task = node の追跡・sync 契約を変えるので今回は見送り、roadmap 扱い。
- WE-18（scheme を閉じる手段が無く、abandon した replica を run_rounds が新シードで復活させる）: `close_rounds(job_dir, scheme_id, reason)` を追加（`scheme.closed = {at, reason}`）。`run_rounds` は `rounds_scheme_closed` で拒否、`inspect_rounds` と envelope の `next` は done。failed の最新 attempt が `node_abandoned` の replica は retired 扱いで再走せず、round はそれ抜きで完了（WE では weight 和 ≠ 1 で `we_resample` が拒否するので、WE は close で止める）。rounds のある scheme は置き換え不可（新 id で続ける）。
- WE-19（MPS job が `launch_failed_requeued_held` になると driver が永遠に待つ; c236 / c155 の Prolog error で we1 / we2 が 35 分停止）: rounds の MPS job を `--no-requeue` で投げる（起動失敗は FAILED / NODE_FAIL で終わり、既存の retry に乗る）。加えて `check_job` が pending reason（squeue `state_reason` / `%r`、scontrol `Reason`）を返すようにし、driver は held を検出: `launch_failed_requeued_held` は `scontrol release` を 2 回まで、それでも held なら cancel（segment は failed → 新シードで retry）、JobHeldUser / Admin は warnings に出して待つ。
- 課題 A の WE 完了（00:08 JST）: we1（70 round、2,023 ns）k_AB = 4.19e7 [1.26e7, 7.20e7] s⁻¹、we2（64 round、1,978 ns）2.80e7 [0.17e7, 7.24e7]; brute force（1.26 µs、46 事象）3.85e7 [2.82e7, 5.13e7]（同じ到達判定）、≥20 ps 滞在なら 3.26e7。区間は重なるが verdict は両方 flux_transient。
- WE-23（速くてスパイキーな flux では定常でも指数フィットが決まらず flux_steady に届かない: we1 τ = 95 ± 560 ns、窓が最後の 1/4 に落ちて 3/4 のデータを捨てる）: `steady_state_rate` にフィット非依存の定常判定を追加。フィットが決まらないときは、run 末尾の「Mann–Kendall で有意な傾向が無く（p ≥ 0.05）、≥ 1/4 run、事象 ≥ min_events」の最長区間（`stationary_suffix`: 候補開始点を新しい方から歩き、傾向が出た時点で止める）を窓にして flux_steady、bootstrap block は窓の積分自己相関時間、`window.stationarity` に検定結果。テスターの手計算（we1 round 11–70、49 事象、p = 0.10 → 2.9e7 [1.7e7, 4.0e7]）が公式値になる見込み。テスト 3 件。A の延長はしない（ユーザー決定）: brute force 完了（04:40）を待ってまとめる。
- テスター確認 #7（00:35 JST）: WE-23 で we1 は flux_steady 3.05e7 [1.67e7, 3.74e7]（round 7–70、51 事象、MK p = 0.69）、we2 も flux_steady だが窓が round 49–64（16 round）で区間 [0.17e7, 7.24e7] のまま; pooled 2.93e7 ± 0.13e7（SEM）、MFPT 34 ns。brief の合格目安（両 scheme が steady かつ brute force の区間と重なる）を満たした。
- WE-23b（末尾から歩いて最初の傾向で止める探索は、途中のバースト（we2 の round 42–55）で窓を不必要に短くする）: `stationary_suffix` を「最初の recycle の次の round 以降で、条件（p ≥ α、事象 ≥ min_events、長さ ≥ 1/4）を満たす最も早い開始点」に変更。立ち上がりは中の全開始点で失敗して後ろでだけ通るので最も早い合格は立ち上がりの後に落ち、バーストはその位置の開始点だけ失敗するので長い窓が残る。`candidates_tested` を出力。テスト追加（バースト）。
- テスター確認 #8（00:33 JST、WE-23b 後の課題 A 最終値）: we1 3.05e7 [1.67e7, 3.74e7]（round 7–70、51 事象）、we2 2.22e7 [1.02e7, 4.51e7]（round 7–64、46 事象; 修正前は round 49–64 で [0.17e7, 7.24e7]）、pooled 2.64e7 ± 0.41e7、MFPT 38 ns。brute force（1.26 µs 時点）3.85e7 [2.82e7, 5.13e7]（同じ到達判定）/ 3.26e7（≥20 ps 滞在）/ 3.16e7（0.5 ns ごとの終点判定）に対し 0.69–0.84×、区間は重なる。両 scheme とも block_capped（積分自己相関 > 窓/5）で区間は楽観的と明示。analyze_009–011。
- 課題 B の WE 100 round（報告 #8、03:50 JST）: unf1（folded → q ≤ 0.2）flux_steady 2.95e6 [1.80e6, 4.19e6]（フィット経路、round 17–100）、fold1（500 K 伸長 basis → q ≥ 0.8）flux_steady 1.68e6 [0.84e6, 2.84e6]（定常区間 29–100）。brute force（1.31 µs）は WE と同じ終点判定で k_unf 5.3e6、滞在 ≥ 0.2 ns で 2.6e6 → 因子 2 以内、文献（CHARMM22*、4.5e5）より 4–10 倍速い（力場差の範囲）。fold1 は basis の定義（伸長鎖 1 本）が brute force の短い変性揺らぎと別 ensemble で比較不成立（文献 τf = 0.6 µs との一致は偶然扱い）。B の WE は 41 GPU-h（見積 15 の 2.7 倍: walker 150–170/round、起動 11 s/segment）。累計 86 GPU-h。B の延長はしない（ユーザー決定）。
- WE-24（窓の選び方が到達の遅れ・初到達直後の行き過ぎ・ノイズ下の上昇に弱く、B の推定が窓次第で 1.5 倍動く）: (1) フィット経路の burn-in 2τ を最初の recycle round から数える、(2) 窓の「最初の 1/4 vs 残り」を block bootstrap 区間（または 20 %）で比べる level check を両経路の窓条件に追加（stationary_suffix の候補にも適用）、(3) フィットの plateau と窓平均が食い違っても窓が level かつ無傾向なら窓平均を採用（plateau は行き過ぎを表せない rise model の artefact）、(4) `window.sensitivity`（開始を窓の 1/4 ずつ遅らせた平均）と `window.level_check`、`window.path` を出力。実データ較正: we1 7–70 で 3.05e7（不変）、we2 7–64 で 2.22e7（不変、バースト耐性維持）、unf1 36–100 で 2.10e6 [1.27e6, 2.88e6]（17–100 の 2.95e6 から; テスターの開始 40–60 手計算 1.9–2.1e6 と一致）、fold1 52–100 で 2.14e6 [1.75e6, 3.33e6]（29–100 の 1.68e6 から; 手計算 2.1–2.5e6 と一致）。`pcoord-and-bins.md` に「basis の定義が測る k_fold を決める」注記。
- テスター確認 #9（04:20 JST、WE-24 後）: unf1 2.10e6 [1.27e6, 2.88e6]（round 36–100、467 事象、MFPT 477 ns）、fold1 2.14e6 [1.75e6, 3.33e6]（52–100、185 事象）、A は不変（we1 3.05e7、we2 2.22e7、pooled 2.64e7 ± 0.41e7）。テスターの観察: A では窓の開始を遅らせるほど rate が上がる（we1 3.05 → 4.0e7、we2 2.2 → 3.0e7; level check の許容内）— 中間 bin の重みの遅い緩和の可能性、brute force の 3.85e7 に近づく方向。バグではなく観察として report に記載。累計 87.5 GPU-h。
- 課題 A 確定（04:40 JST、brute force 4 × 500 ns = 2 µs 完了）: brute force k_AB = 3.53e7 [2.73e7, 4.48e7]（αL 滞在 ≥ 20 ps、67 事象、MFPT 28.4 ns; 滞在なし 4.05e7、0.5 ns ごとの終点判定 3.25e7、run 間 SE 0.54e7）。WE pooled 2.64e7 ± 0.41e7（we1 3.05e7 [1.67e7, 3.74e7]、we2 2.22e7 [1.02e7, 4.51e7]）は brute force の 20–35 % 下、両 scheme とも flux_steady で区間は重なる → brief の合格目安を満たす。WE が低めなのは定常状態への遅い接近（窓を遅らせると 4.0e7 まで上がる）。文献比較: Vitalini 2015 方式（36×36 φ/ψ グリッド MSM）で t2 = 1.39 ns vs ff99SB-ILDN 1.27 ± 0.17 ns（一致）、t3 = 0.12 vs 0.073 ns（OPC の粘性）。brief の brute force 定義（φ > 30°）は滞在なしだと ±180° wrap の偽事象 10,040 件を数えるので、20 ps 滞在が必要（brief に注記）。A の費用 35.7 GPU-h（WE 23.5: local 5.0 + MPS 18.5、brute force 12.1）。
- WE-20 クローズ（04:53 JST）: settled read 導入（14:43 UTC）以降 15,852 segment が失敗なしで完了。B の文献比較用に、テスターが Lindorff-Larsen の SOM の事象定義（6 本の長距離 Cα 接触の Q、10 ns 移動平均、二重閾値 0.9 / 0.1、状態寿命）で brute force を再解析するスクリプト（cln025/analysis/bf_q_desres.py）を用意（基準距離は eq_001 ではなく folded frames の中央値）。速報（~320 ns/run）: k_unf ≈ 1.8e6 [0.21e6, 6.4e6]（2 事象、変性時間 ~0.57 µs）— WE の 2.10e6 に近く、CHARMM22* の 2.2 µs より ~4 倍速い。折り畳み事象はまだ 0。
- ユーザー決定（9/22 昼）: WE-3（segment の runtime_system.xml / checkpoint 省略）は見送り。WE-17 は実装: `run_segment_batch`（k 本の segment を 1 プロセスで順に実行、各 segment に owner record + heartbeat、未到達は pending のまま再送、実行中に task が死ねば stale → retry）と `run_rounds --mps-segments-per-task k` / `--mps-max-jobs`（既定 8）。k の既定は自動: round の pending が `tasks_per_gpu × gpus × max_jobs` の slot を埋めるまで k = 1、埋まって初めて k = ceil(pending / slot 数)（上限 8）— 特定の GPU に k 本を偏らせず、まず GPU を埋める（ユーザー指摘）。task の先頭 node が Slurm 追跡対象、残りは owner record。再開時は batch 実行中の segment を heartbeat で待つ。テスト 5 件（`TestSegmentsPerTask`、`TestRunSegmentBatch`）。実機の効果測定は次のキャンペーンで（見積: B の 0.2 ns segment で GPU 時間 25–35 % 減）。
- Phase 3 完了（9/23 00:55 JST、テスター最終報告）: A・B とも brief の合格目安を満たした。B の brute force（4 × 1 µs）: 滞在 ≥ 0.2 ns で k_unf 3.96e6 [1.81e6, 7.53e6]、k_fold 2.89e6 [0.94e6, 6.75e6] → WE（2.10e6 / 2.14e6）は因子 1.9 / 1.35 で一致; Lindorff-Larsen 方式の Q 解析で τu 0.32 µs（CHARMM22* 2.2 µs）、τf 0.58 µs（同 0.6 µs）; 340 K の K = 1.0–1.4（実験 ≈ 1.15、CHARMM22* 3.7）。最終台帳 107.7 / 350 GPU-h（A 35.7、B 72.0、MPS job 4,277 本）。DAG: B 26,120 ノード（progress.json 13.7 MB、events 11 万件、segment 計 ~270 GB）、A 8,302 ノード。フィードバック 29 件: 22 修正確認、1 doc のみ（WE-5）、2 実装済み未実行（WE-17、19）、1 見送り（WE-3）、3 open → 下で対応。report: `/data1/rkp00079/rku00161/we-trials/report.md`（§9 が memo 用要約）。
- キャンペーン終了（9/23 01:31 JST）: WE-25 をテスターが実機確認（`inspect_rounds` fold1 298 s → 8.6 s、aggregate 2,664.4 / 2,450.2 ns 不変）。4 scheme を `close_rounds` で閉じ（ユーザー決定、reason「WE Phase 3 test campaign complete」）、`next = done` を確認。閉じたときの書き込みで progress.json が初めて compact になり A 4.27 → 2.67 MB、B 13.71 → 8.65 MB（−37 %、ノード数不変）。GPU ジョブ・driver・監視はすべて終了、最終費用 107.7 / 350 GPU-h。
- WE-25（`inspect_rounds` が完了 segment ごとに node.json を読み 13,300 segment で 298 s）: 集計を「完了 segment 数 × stage_args.simulation_time_ns」に（`aggregate_ns_source: stage_args`; 長さ不明の stage tool だけ従来どおり）。
- WE-26（ノード操作ごとの progress.json 全体書き直しで 26k ノード級では driver 側が round の 9 割）: (1) progress.json を compact JSON で書く（13.7 → 8.7 MB、dumps 0.20 → 0.04 s）、(2) `lifecycle._create_nodes_bulk`: round の segment を lock 1 回・index 書き込み 1 回で作成し、driver 作成ノードは `explain_node` preflight を省略（26k ノードの index で 1 ノード 0.22 s → 0.003 s、実測 40 本）。残る per-segment の書き直しは Slurm stamp と tool 側の begin / complete（2–3 回 / segment、packed task 間で lock 直列）で、10⁵ ノード級には index の分割（scheme 別 sub-index か journal）が要る — roadmap の Known Issues に記載。
- WE-16b（owner の Slurm job id で即時 stale 判定）: `owner_liveness` が `squeue` のあるホストでは owner の job を照会し、終了済み / 不明なら heartbeat を待たず stale、queue にあれば alive（squeue 失敗時は heartbeat）。テスト追加。
- WE-21（縮退したフィットで bootstrap の block が窓全体になり 95 % 区間が幅 0）: `_bootstrap_block` で block を「窓の 1/5 以上のブロック数が取れる長さ」に上限（`MIN_BOOTSTRAP_BLOCKS = 5`）、`window.block_capped` と verdict_reasons に「区間は楽観的」と明示。`bootstrap_statistic` も同じ上限。
- WE-22（launcher 経由の policy に owner record が無い）: `_run_policy_mps` の launcher 分岐でも subprocess の前後で owner.json + heartbeat を持つように修正。WE-18 の文言（replicas scheme に「last policy node」を案内）は `_analysis_hint(policy)` で分岐（replicas は segment を解析）。
- テスター確認 #6（22:45 JST）: WE-22 は実機で再現確認（policy 実行中の driver を子プロセスごと SIGKILL → 同 host の `inspect_rounds` が即 stale、再起動で `rounds_owner_lost` → `_t0001` が同じ resampling seed で完了、round 32 以降続行）。WE-21 は unf1 の analyze_003 で block 1 / block_capped、区間 3.3e6–8.9e6 s⁻¹（flux_transient、窓内事象 41）。WE-18 文言 OK。B は round 31 から 100 へ再開、累計 33.1 GPU-h。
- テスター確認 #5（22:30 JST）: WE-18 OK、mps driver の kill → 再実行で 13 job を再投入せずに待って続行 OK、WE-19/20 は driver 再起動後に再発なし（held の経路は自然発生待ち）。B の WE は round 100 まで続行（ユーザー決定、~15 GPU-h、B 計 ~55 GPU-h）。累計 25.6 GPU-h / 350。
- 上の続き（23:20 JST）: driver 再起動後も 4 本（unf1 r0035、we1 r0030 / r0040、we2 r0016）が同種で失敗。code は `input_resolution_blocked`「progress.json is missing or invalid」と `node_execution_context_invalid`「Node … is missing from progress.json」で、いずれも `_load_progress_v3` の `exists()` が False を返した形（index は再確認でも無傷: cln025 10,247、aladip 6,220 でディレクトリ数と一致）。結論: Lustre 上で別ホストの rename が着地する瞬間、lock を持たない読み手には path が一瞬見えない。対処は読み手側に一般化: `node/io.py::_load_json_settled`（5 回・0.1 s）を `_load_progress_v3` / `_read_node_json_path` / `read_node` / driver の `_node_exists` に通し、`create_if_missing=True` でも一時的な miss で index を初期化しない（初期化されると index が全消えする危険があった）。`docs/developer/roadmap-and-known-issues.md` の Resolved に記録。
- unf1 / fold1 で各 1 本出た `slurm_failed` の原因: compute node 側の CLI preflight が progress.json を読めず（`_load_nodes` が例外を握りつぶして空 index）、親を "missing" と誤判定して `parent_not_completed` で拒否（exit 1 → slot FAILED → failed → retry）。index 自体は無傷（3,552 ノード = ディレクトリ数、status 不一致は in-flight のみ）、/data1 は Lustre（`flock` マウント、全 writer が `progress.lock` + atomic rename）なので、読み取り側の一時的な失敗。対処: preflight は `_load_nodes_strict`（5 回・0.4 s 間隔で再読）にし、それでも読めなければ `progress_unreadable`（node は pending のまま、同じコマンドを再実行）を返す。原因の切り分けは次に出たときの message（例外文言入り）で。
- テスターの mps 実測（we2、aladip 0.5 ns segment）: round 68–100 s、packed job 54–70 s、poll 遅れ 8–22 s、policy 6–7 s、8 本 packing で segment の GPU コスト 33 s → 9 s（3.6 倍安い）。
- 未実測: RIKYU 実機の throughput とノード 1,000 級の DAG オーバーヘッド（テスターが we1 の続きと we2 で取る）。`--mps-time-limit` は packed segment 1 本の時間 + margin（TIMEOUT は failed → retry なので、短すぎると round を浪費する）。

## 2026-09-21 — round 駆動サンプリング（`mdclaw/rounds/`）と Weighted Ensemble（`mdclaw/we/`）を DAG に実装（Phase 0–2、CPU テストまで）

計画は `docs/research/weighted-ensemble-plan.md`（ユーザー案「walker = prod ノード、分裂の判断は prod 群を親にした analyze ノード、末端の analyze で kinetics」を、ユーザー方針「WE 専用仕様を避け、既存 DAG を使い、他の MD 手法でも使い回す」で汎用の round 駆動に組み替えた版）。実装は縦に薄く通した:

- **Phase 0（汎用の下回り）**: `create_node` に非公開引数 `_node_id`（構造化 id `<type>_<scope>_<letter><4桁>...`、`STRUCTURED_NODE_ID_RE`、連番割当は非数値接尾辞を無視するので同居できる）と `_metadata`（作成時 metadata。`conditions` はツールが報告しないキーを拒否する契約なので、round・replica・重みはここに置く。`_apply_status` は dict 併合なので完了後も残る）。`dag_snapshot` / `node_missing` / `parent_required` の id 列挙を `ID_LIST_CAP`=40 で打ち切り（先頭 20 + 末尾 20、`truncated` に省略数）。`run_production` の兄弟 seed 衝突ガード `production_sibling_seed_collision`（実効シードは `_restart_random_seed(seed, restart_step)` だけで決まるので、同じ祖先から同じ seed の完了済み兄弟があると同一軌道になる。`--allow-seed-reuse` で意図的な再現は可。ノードは pending のまま）。
- **Phase 1（`mdclaw/rounds/`、registry 名 `rounds`）**: `setup_rounds`（scheme を `progress.json.params.sampling_schemes[id]` に記録。policy = `replicas` 内蔵 / analyze ツール名、stage_tool = prod ツール、stage_args、start.node_ids × n_replicas、initial_weights、seed）、`run_rounds`（round ループ: pending segment をこのプロセスで stage tool 関数呼び出し → 失敗 replica は `_t000N` 兄弟に新 seed で再走（3 回で `rounds_replica_unstable`）→ policy ノード `analyze_<scheme>_r<round>`（親 = 完了 segment 群、dependency = 前 round の policy）を作って policy ツールを走らせ `next_round.json` を読む → 次 round の segment `prod_<scheme>_r<round>_w<replica>` を `continue_from` 親 segment / `parent_node_ids=[start]`、`dependency_node_ids=[policy]` で作成。`max_rounds` / `max_aggregate_ns` / `max_wall_hours` / policy の stop で round 境界で返り、DAG だけから再入）、`inspect_rounds`。`next_round.json` の契約は `rounds/plan.py`。envelope の `next` は scheme ノードなら常に `run_rounds`。`run_production` は分割せず関数呼び出しで使い回す（ユーザー判断: 小系なら τ = 100–200 ps でオーバーヘッド 2–3 割の見込み、実測は Phase 3）。
- **Phase 2（`mdclaw/analyze/cv.py`、`mdclaw/we/`、registry 名 `we`）**: CV 評価 distance（分子内 raw / 分子間 最小像、`find_molecules` で判定）・rmsd（`align_selection` で「タンパク質で合わせてリガンドで測る」）・dihedral（度）・q（Best–Hummer–Eaton、`analyze_q_value` と同じ定義）。分子内の q / dihedral は `periodic=False`（mdtraj の既定は最小像で、箱より長い鎖の接触が折り返されて Q≈1 になった）。`we_resample`（policy: bin 割当 → target 内を basis に再循環 → bin ごとに最軽量 2 本併合（生存者は重み比抽選）/ 最重量を等分割、Σw 検算 1e-12、`we_round.json` 台帳、併合された walker には `we_merged` event）、`analyze_we`（flux の指数フィットで `flux_steady` / `flux_transient` / `no_target_events`、last-quarter 平均 + moving-block bootstrap、MFPT、箱体積から M⁻¹s⁻¹、再循環なしは 2 状態フィット、`we_bins.csv` の −kT ln P、`we_frames.csv` の frame 重み）。
- **テスト**: `test_node`（構造化 id）、`test_envelope`（上限）、`test_production_seed_guard`、`test_rounds`（2 原子周期系で replicas 3 本 × round、中断再開、policy 経由の分裂・再循環・stop、失敗再走、wall-time）、`test_we_resample`（resampler と CV、合成 mdtraj 軌道）、`test_we_kinetics`（フィット + **1 次元格子の二重井戸で本物の resampler を再循環つきで 320 round 回し、定常 flux が厳密 MFPT の逆数と 30 % 以内**）、`test_we_nodes`（rounds + we_resample + analyze_we の通し）。関連スイート 709 本 pass、ruff クリーン、golden（guardrail 457 code、CLI 契約 95 ツール）再生成。
- **CLI の副産物 2 件**: (1) `we_resample` を CLI で回すと mdtraj の DCD reader が C の stdout に出す `dcdplugin) …` 行が、C stdio のバッファ経由で結果 JSON の**後**に流れてパーサが壊れた（memo 2026-09-18 の「mdtraj dcdplugin が CLI stdout を汚す」と同じ根）。`_cli.py` にツール実行中だけ fd 1 を stderr に複製し、結果を出す前に `fflush(NULL)` して戻す `_NativeStdoutToStderr` を入れた。analyze 系ツール全部に効く。(2) seed 衝突ガードは「バイアス無しの兄弟」に限定: PLUMED / steering / 拘束つきの兄弟は別 System なので同じ seed でも同一軌道にならず、`tests/test_plumed.py` の「同じ seed で同じ steering を再実行して state を検証する」使い方を誤って拒否していた。
- **既存 WIP 由来で落ちるテスト 2 本（本作業とは無関係、未修正）**: `test_pdb_export_resname_guard::test_pdb_writefile_inventory_is_pinned`（未追跡の `simulation/expanded_ensemble.py` の `PDBFile.writeFile` が台帳に無い）と `test_restraint_selection::test_direct_distance_restraint_reporter_matches_custom_cv_reference`（staged の `restraints.py` `groups_share_molecule` が結合の無い topology で `find_molecules` の ValueError を素通し。`cv.py` は同じ判定を try/except で囲んである）。
- **スキル**: `skills/md-we/`（SKILL.md + pcoord-and-bins.md + kinetics.md）、`skills/md-production/rounds.md`（replica）、md-production / md-analyze / hpc-run からの誘導。
- **Phase 3 テスター（Claude Code、Herdr pane）の test-run feedback WE-1〜14（同日 16:40–19:55）に対応**: WE-1 `run_production` の metadata に `md_seconds` / `wall_seconds` / `ns_per_day`。WE-2 `setup_rounds` が `we_resample` の policy_args を setup 時に検証（CV のコンパイル、start 構造の pcoord 評価 → `scheme.start_pcoords`、target 内なら `we_start_in_target`、分子間距離は半箱長）。WE-5 変性 basis の作り方（`--restraint-force-constant 0` の eq 鎖）を `pcoord-and-bins.md` に。WE-7 `inspect_rounds` に round ごとの policy_summary と `busy` / `wait` の next_action。WE-8 `analyze_we` 完了後の `next` は verdict が steady 以外なら `run_rounds --max-rounds <next_rounds_suggested>`。WE-9 verdict にプレートー決定（f_ss_err/f_ss < 0.5）と窓内事象 ≥ `min_events`（既定 10）を要求、窓は burn-in（2τ）以降の全 round（Q4）。WE-10 2 次元 bin のヒートマップと `we_bins.csv` の lo/hi。WE-11 per-molar は分子間距離のときだけ。WE-12 `analyze_parent_invalid_type` の message / next_action。WE-13 launcher `bin/mdclaw` が引数中の絶対パスの親を bind（job_dir が CWD 外だと「progress.json がない → bootstrap せよ」と誤誘導していた）、`rounds_job_dir_unreachable`。WE-14 `next_rounds_suggested` は決まらないフィットからは出さず、事象頻度から算出して上限 50。WE-13b 到達できない job_dir では envelope の `next` を DAG から作らず `blocked` に。WE-15 verdict に `flux_undersampled`（窓の事象 < `min_events`: `rate` は null、窓平均は上下どちらにも外れうるので引用しない）を追加し、`flux_transient`（下限）は事象が十分で flux がまだ立ち上がり中のときだけに限定（テスターの例: 事象 3 回の窓平均 9.4e7 s⁻¹ は brute force 3.4–4.0e7 の 2.5 倍「上」で、下限ではなかった）。テスターの途中値: alanine dipeptide φ 反転の brute force（4 × 133 ns）k_AB ≈ 3.4–4.0e7 s⁻¹、k_BA ≈ 5.5–6.4e8、2 状態 t2 ≈ 1.5–1.7 ns（Vitalini 2015 の ff99SB-ILDN 1.27 ± 0.17 ns）; WE は 1 round ≈ 0.5 GPU-h、segment の固定オーバーヘッド 0.27 s（1 %）、DAG 操作 0.35–0.45 s/segment。
- **未了**: WE-3（segment の `runtime_system.xml` / chk 省略）、WE-4（executor `mps`）、Phase 3 の結果（Ala3 / CLN025）、retention、一括版。

## 2026-09-21 — test-run feedback 25–26: `analyze_fep` が `charge_correction` を返す、`generate_md_report` が既存 `--output-dir` に再実行できる

- **25（確認、修正）**: `estimate_ddg` は `hybrid_manifest.json` を読み直して両 leg の `charge_correction` を比較していたが、`analyze_fep` の結果にも `fep_result.json` にも出ていなかった（`restraint` は出ていた）。`_charge_correction_of` が manifest の `charge_correction` + `charge_correction_detail`（`charge_change_e`、co-ion の `lambda_range` / `window_indices`）をツール結果・`fep_result.json`・node metadata（`method` のみ）に載せる。direct mode は protocol と同じディレクトリの manifest を読む。manifest が読めなければ `null`（「補正なし」と区別）、co-ion 以前の manifest は `method: none`。`_leg_settings` は manifest が失われたとき leg 結果の値にフォールバックする。overlap の落ち込みが co-ion 窓の内か外かを leg 単体で切り分けられる（`skills/md-fep/convergence.md` に 1 行）。
- **26（確認、修正）**: `reporting.py` の `mkdir(exist_ok=False)` は意図的な上書き防止でテストもあったが、書くのは再生成可能な `report.json` / `references.bib` の 2 つだけで、再実行不能 + `report_invalid_input` という誤解を招くコードの害の方が大きい。`exist_ok=True` にし、置き換えたファイルを `files.replaced` で返す。ディレクトリ内の他ファイルには触れない。node ディレクトリ配下の拒否は維持。これは「既存ディレクトリは拒否」という従来仕様（tool-reference の記述と `test_runtime_citations_not_from_declarations` の該当 assert）を覆す。
- 検証: SIF で `ruff check mdclaw/ tests/` 通過、`tests/test_fep.py tests/test_evidence_server.py tests/test_envelope.py`（not slow）119 passed。

## 2026-09-21 — 1 ジョブ 2 leg DAG と co-alchemical ion の実測検証 (I70A / T127I / A14D)

`65a5918` 以降の「両 leg が 1 ジョブ」DAG と `92a4d8a` の荷電変異補正を、前回とは別の
3 変異で検証した。スタディは `/home/yasu/tmp/fep_bench_v2`、詳細は同ディレクトリの
`RESULTS.md`。21 窓 x 2 ns/leg、ff19SB + OPC、合計 672 ns、fep ノード 16 個。

**ddG (kcal/mol, 正 = 不安定化)**

| 変異 | MDClaw | 参照 | 差 |
|---|---:|---:|---:|
| I70A | +4.665 +/- 0.115 | +4.353 | +0.312 |
| T127I | -1.929 +/- 0.155 | -2.325 | +0.396 |
| A14D (co-ion) | +3.282 +/- 0.469 | +1.793 | +1.489 |
| A14D (補正なし) | +3.248 +/- 0.326 | +1.793 | +1.455 |

3 変異で r = 0.9821, MAE = 0.732, RMSE = 0.907, 符号一致 3/3。中性 2 変異は 0.31 / 0.40 で
v1 の 4 変異 (MAE 0.237) と同水準。**側鎖が伸びる T127I (消失 6 / 出現 11) でも精度は
落ちない**。v1 と同じ offset 相殺も成立し、folded / unfolded のオフセット差がそのまま
ddG 誤差になっている (I70A 7.729/7.417、T127I 6.185/5.790)。

**co-alchemical ion の初回実測**: ddG への影響は **+0.034 kcal/mol** で、合成誤差 0.571 に
対して区別できない。leg 単体は -83 -> -162 kcal/mol と Na+ 生成自由エネルギーぶん動くが
ddG では相殺する。この箱サイズ (folded 7.674 nm / tripeptide 3.262 nm) では PME 中和背景の
誤差が両 leg でほぼ等しかった、ということ。コストは誤差 1.4 倍 (0.326 -> 0.469) と
min overlap 約 3 割悪化 (0.053/0.073 -> 0.038/0.036)。

**overlap の劣化は相境界に限局**: 隣接 overlap が落ちるのは index 7 と 14 の 2 点のみ。
`--phase-bounds 0.35,0.75` の 21 窓では index 7 = lambda 0.35 = p1、index 15 = 0.75 = p2 で、
co-ion が `fep_core` に乗って現れ始める / 終わる境界そのもの。unfolded 側も同位置で再現
(0.036, 0.041)。閾値 0.03 は上回るが余裕は小さく、lambda を 0.35 / 0.75 近傍で密にするのが対策。

**選ばれた水**: folded `site_distance_nm` 6.632 (箱の最小像上限 6.646)、tripeptide 2.789
(上限 2.825)。どちらも最遠点を選べており最小像規約も効いている。`fep_coion_box_too_small` は
出なかったが、tripeptide は 1.5 nm 要件に対し 2.789 nm なので `solvate_structure --dist 10` が
実質の下限。

**運用面**: `configure_container` の実行忘れで 18 ジョブが `mdclaw: command not found` で
全滅した事故から、`stranded_jobs` -> `--clear-slurm-metadata` -> `--abandon` で復旧。GPU 競合
(kawai の非 SLURM ジョブと同居) で I70A の unfolded leg が 12 時間制限に届かず、`cancel_job`
-> `--clear-slurm-metadata` -> `--restart-windows-file` で完了済み 6 窓 (7.3 h 相当) を保存した
まま救出し、全 21 窓を回収した。クリーンな A6000 では 435 s/窓 (62,335 原子, 2.1 ns) =
207 s/ns で v1 の 210 s/ns と一致。共有時は 4.7-10 倍遅い。実行期間中、クラスタの GPU 50 枚は
すべて割当済みだった。

**md-report**: ddG ノードを対象にすると両 leg が lineage に入り、topo ごとに
`charge_correction` が記録され、`Chen2018ChargeChangingFEP` は co-ion 実行のレポートにだけ
現れる (中性 2 変異と A14D 補正なしには入らない)。

---

## 2026-09-20 — test-run feedback 23–24: `inspect_cluster` の GPU 在庫（複数型ノード、空き枚数、トップレベル）と、T4L 検証の中止（branch `fix/inspect-cluster-gres`）

- **24（バグ、確認）**: GRES のパースが `re.search` で最初の 1 件しか読まず、`m2  gpu:3090:1(S:0),gpu:a5000:1(S:0)` の a5000 が落ちていた（`gpu_types` に無い、`total_gpus` 49）。`_parse_gpu_models` が全エントリを `{型: 枚数}` で返す（`Gres` と `GresUsed` の両方に使う）。実クラスタで 7 型・50 枚になり、テスターが `scontrol` から数えた正解と一致。
- **23（半分正しい）**: `gpu_inventory` / `node_gres` は `partitions[i]` の下にはあり、トップレベルに無かった（テスターが見た「空」はトップレベル）。警告文が場所を言わずに「see gpu_inventory / node_gres」と案内していたのが実際の問題。トップレベルにも出し（**物理ノード単位**で集計するので、複数 partition に属するノードの GPU は 1 回だけ数える。`total_gpus` も同じ）、警告文に型ごとの要約（`a6000: 0/7 free on floyd; …`）を入れた。
- **空き枚数を足した**: 型ごと・ノードごとに `gpus_total` / `gpus_used` / `gpus_free`（`GresUsed` を同じパーサで読む。サイトが `GresUsed` を返さないときは `null` — 「0 枚空き」とは言わない）。
- **出力がノード数に比例して膨らまないようにした（ユーザー指摘: 「Rikyu でやったらとんでもないことにならない？」）**: 最初の版は型ごとの全ノード名を警告文 1 本に連結し、トップレベル `node_gres` をノードごと 1 行で返していた（以前からの `partitions[].node_list` / `node_gres` も同じ作りで、今回それを複製してしまった）。数千ノードのスパコンでは警告文と `result.json` / `.mdclaw_cluster.json` が MB 級になる。32 ノードを超えたら、ホスト名は Slurm の範囲表記に畳んで 8 件まで（`rk[0001-3000]`、`+N more`。`--nodelist` にそのまま使える）、`node_gres` は GRES 文字列ごとのグループ行（ノード数・使用量の合計、`node_gres_grouped: true`）、`gpu_inventory` に `nodes_with_free_gpus` / `free_node_list` を足して「どこが空いているか」はノード表なしで答えられるようにした。集計は畳む前の生データから行う。3000 ノード × 2 partition のテストで出力・設定ファイルとも 6 kB 未満。32 ノード以下のクラスタでは従来どおりノード名とノード別の行を返す。
- **これが要ると分かった経緯（自分の誤判断の記録）**: T4L L99A / ベンゼンの ABFE 検証を自分で回そうとして、`squeue` の表示（1080 を明示要求しているジョブが n2・n4 で各 4 本）から「1080 が 12 枚空いている」と判断し、両 leg のチェーン 7 ジョブを投入した。実際は `GresUsed` で全 50 枚が使用中で（他ユーザーのジョブは gres 表示が `N/A` でも GPU を使っている）、`sbatch --test-only` の開始見積もりは 9/22〜9/27。ユーザーの判断で検証は中止し、投入した 7 ジョブはキャンセルした（ユーザー自身のジョブには触れていない）。`inspect_cluster` が空き枚数を返していれば投入前に分かった。`hpc-run/SKILL.md` に「全型 0 枚空きなら、長いチェーンを投入する前にそう伝える」を追記。
- **T4L 検証の到達点**: `outputs/abfe_t4l/`（git 管理外）に 181L から調製した複合体と両 leg のデカップリング用トポロジーまで（complex 46,457 粒子・端点差 2.8e-5 kJ/mol、solvent 5,404 粒子、ともに端点検証 pass）。実パイプラインでの ABFE トポロジー構築は 3PWB/GOL に続く 2 例目。Boresch 選択がベンゼンで通るか、overlap、dG_bind の値は**未確認のまま**。再開は `outputs/abfe_t4l/submit_pass1.py`。
- テスト: `test_slurm_server.py` に「2 型を持つノード + 2 partition に属するノード」「`GresUsed` が取れないサイト」、既存の混在 partition テストに空き枚数・トップレベル・警告文の要約。slurm / CLI 系 303 passed。

## 2026-09-20 — FEP test-run feedback 21: 実行中の fep ノードが時間制限に収まるかを `check_job` が警告（branch `feat/abfe`）

`--sampling-time-ns` × 窓数 × 時間制限の組み合わせは事前に見積もりにくく、共有 GPU では 5〜10 倍外れる、というテスターの指摘。材料は全部あった: 投入時の tracker 記録の `time_limit`、`check_job` が取る Slurm の経過時間、`run_fep` が窓の完了ごとに書き直す `fep_windows.json` の窓別 `wall_time_s`。

- `fep.run.fep_time_budget`: そのノードが**実測した**完了窓の平均 wall time × 残り窓数（実行中の窓が既に使った分は差し引く）を、時間制限の残りと比べる。要求サンプリング時間からの推定はしない。親から引き継いだ窓（`carried_over`）は数えない。完了窓が 0 のうちは何も言わない。
- `check_job` は `RUNNING` かつ fep ノードに紐づくジョブで `time_budget` ブロックを返し、収まらないとき `time_limit_risk:` 警告を出す。`list_tracked_jobs --sync` は同じ警告を `warnings` に転記（vanished と同じ経路）。見積もりの失敗は状態確認を失敗させない。
- 指摘の前提を 1 点だけ訂正: 時間切れで「丸ごと失う」ことはない（インデックスは窓ごとに書き直され、完了窓は `--restart-windows-file` で回収できる）。警告文もそう書き、「走らせ切って不足窓を新ノードで回収」と「今キャンセルして長い制限で同じことをする」の両方を示す。大きい protocol は制限を延ばすより `--lambda-indices` で複数 fep ノードに分けるのが本筋、と `monitor-recover.md` に記載。
- Slurm の経過時間は `squeue --json` の秒数と `[D-][HH:]MM:SS` の両方を読む（`_elapsed_seconds`）。既存の `_parse_time_limit_seconds` は 2 要素を HH:MM と読むが、Slurm の時間制限の 2 要素は MM:SS（既存の挙動なので今回は触っていない。MDClaw が出す制限は常に HH:MM:SS なので実害は出ていない）。
- テスト: `tests/test_fep_time_budget.py` 9 本（テスターの例そのもの: 21 窓中 6 窓を 7.3 h、制限 12 h → 残り 15 窓に必要な時間 > 残り 4.7 h）。

## 2026-09-20 — リガンドの絶対結合自由エネルギー（ABFE）: `build_decoupled_system` / `add_boresch_restraint` / `extract_ligand` / `estimate_binding_dg`（branch `feat/abfe`）

FEP の次の対象として RBFE より ABFE を先にした（原子マッピングもネットワーク設計も要らず、エージェント側の判断点が少ない。設計指針「スキルは意図、ツールはガードレール」との相性）。設計と判断の全文は `docs/research/abfe-references.md`、手順は `skills/md-abfe/`。

- **既存資産の再利用**: λ 窓サンプリング（`run_fep`）と MBAR（`analyze_fep`）は無変更で使う。変えたのは protocol の契約だけ — protocol が自分の `global_parameters`（既定は hybrid の 5 個、ABFE の complex leg は 6 番目 `fep_restraint`）と `phases` を名乗る（`protocol_parameter_names`、`run_fep` の system.xml チェックと `protocols_equivalent` も protocol 由来の名前で）。リガンドは hybrid の「消える原子」と同じ 2 parameter（`fep_elec_old` / `fep_sterics_old`）で消す。
- **`decouple.py`**: build 済み System を 1 回書き換える（端状態が 1 つなので merge しない）。静電は annihilate、立体は decouple（リガンド内 LJ は別の `CustomNonbondedForce` で常時フル、1-4 の LJ は不変）= openmmtools / YANK の既定。端点検証は「結合状態 = 元の System」と「デカップル状態でリガンドを他原子の上に動かしてもエネルギー不変」。溶媒和 ACE-ALA-NME で両方 1e-4 kJ/mol。
- **Boresch 拘束の置き場（ユーザーと決定）**: complex leg の **eq の下の topo ノード**。`_ALLOWED_PARENT_TYPES["topo"]` に `eq` を足した（auto-parent は solv/prep のままなので通常の topo 作成には影響なし）。拘束は leg の全窓で同一でなければならず、fep は複数ノードに分かれるので、定義を所有するノードが要る。PLUMED は使わない（λ スケールと全窓での再評価が OpenMM の global parameter なら既存ループで動く）。実行時に `run_fep` が足す案は「run 側が System を足し引きする箇所が増える」ので見送り。
- **fep を拘束つき topo の直下に（ユーザー指摘で修正）**: 最初は「fep の親は eq / fep のみ」という型の都合で topo の下に短い再平衡化 eq を挟んでいたが、物理的に不要（窓の開始状態は祖先の最初の eq state で、各窓が自分の λ で最小化・平衡化する）。`_ALLOWED_PARENT_TYPES["fep"]` に `topo` を足し、その代わり eq の祖先を持たない fep は入力解決で `fep_equilibration_required` として拒否（hybrid topo の直下に fep を作って min / eq を飛ばす抜け道を塞ぐ）。auto-parent は `eq` のままなので、親を省略した fep は eq の下に付き `abfe_restraint_required` で topo を名指しして止まる。
- **拘束なしの complex に fep を走らせない**: complex leg の最初の topo は `fep_protocol` を書かず、その下の fep は入力解決で `abfe_restraint_required`。スキルの注意書きではなくツールで止めた。
- **原子選択はツール側で完結**（`select_boresch_restraint`）: eq state から 200 ps 走らせ、受容体主鎖 (N, C, CA) × リガンドの結合重原子 3 つ組を、6 座標の揺らぎ（熱的幅単位）で採点。θ が [40°, 140°] 外は除外、最良でも std(r) > 0.15 nm か角度 std > 25° なら `abfe_restraint_unstable` で拒否。
- **テストが捕まえた実装ミス 2 件**: (1) 二面角の符号が OpenMM と逆（測った基準値でエネルギーが 347 kJ/mol、0 になるべき）。(2) 解析補正の符号と大きさは配置積分の数値積分と 0.15 kJ/mol 以内で一致（既定の K、r0 = 0.5 nm で +33 kJ/mol ≈ +7.9 kcal/mol、文献の典型値と整合）。
- **実パイプラインで判明した不整合 3 件**（3PWB + GOL、既存の `fetch_structure → prepare_complex → solvate_structure` をそのまま使用）: (a) `ligand_chemistry` の `chain_id` は内部ラベル（`Ax4`）で merged.pdb の鎖 ID（merge が振り直した `C`）とも author chain（`A`）とも違う → 残基名 + 残基番号で照合、`--ligand` の鎖は author chain も受ける。(b) `topology.pdb` は CONECT を持たないので読み戻したリガンドは無結合 → 結合は System の結合項と拘束から取る（`bonded_pairs`）。(c) 記録内の SDF パスは DAG が読み出し時に絶対化・書き込み時に `../prep_001/…` へ相対化するので、子 prep へのコピーは不要（最初に書いたコピー処理は死にコードだったので削除）。荷電リガンド（BEN +1）の拒否は最初ビルド後にしか出ず node が failed になったので、prep 記録の電荷でビルド前（pending のまま）に拒否するよう前倒しし、割り当て電荷での判定を後段の保険に残した。
- **e2e**: 両 leg を `estimate_binding_dg` まで完走（complex 23 窓: restrain / decharge / decouple_sterics、solvent 18 窓、eq の下の topo → 再平衡化 eq → fep の入力解決も実走）。1 窓 4 ps なので **出た −5.1 kcal/mol に物理的な意味はない**。物理の検証（T4L L99A / ベンゼン、実験 −5.19 kcal/mol）は別テスターが実施 — 手順書は `abfe-references.md` §7。ベンゼンは空洞内で面内回転するので `abfe_restraint_unstable` のしきい値（25°）が最初に試される。
- v1 の範囲外は拒否コードで止める: 荷電（co-ion を流用すれば対応可能、まず中性で検証）、共有結合、重原子 3 未満、複数コピー、受容体が蛋白質主鎖を持たない系。リガンドの LJ 長距離補正は未実装（hybrid と同じ既知の近似だが、ABFE では 2 leg の周囲密度が違うので相殺は不完全）。
- envelope の `next`: `analyze_fep`（ABFE leg）完了後に「solvent leg を `extract_ligand` で始める」「両 leg 完了なら `estimate_binding_dg`」を案内（ddG の folded/unfolded・complex/apo の役割判定は従来どおり）。引用 3 件（Boresch 2003 / Gilson 1997 / Mobley 2007）を Crossref で確認して登録、ABFE の topo に pmx の引用が付かないよう選択条件を修正。guardrail 14 コード追加（423 codes）、CLI 契約 89 tools。

## 2026-09-20 — 電荷変化変異の co-alchemical ion（`mdclaw/fep/coion.py`、branch `main`）

電荷の変わる変異（K→A, A→D など）を、これまでは「PME の一様背景電荷、有限サイズ補正なし」の警告だけで通していた。両 leg で同じ Δq でも、誤差は箱の大きさと中身（水の数密度、溶質の低誘電体積）に依存するので folded と unfolded で相殺しない。Rocklin 型の事後補正は、APBS 依存・代表構造や誘電率の判断点が増える（間違えても数値が出る）ため**採らない**とユーザーと決めた（解析項だけの併記もやめる）。代わりに箱電荷を両端で同じに保つ。

- **方式**: 変異体端状態の System で、変異部位から最も遠いバルク水の O をイオンの (q, σ, ε) に、H（4 点水の EP も）を電荷 0・ε 0 に書き換えてから hybrid を組む（Chen et al., JCTC 2018）。`HybridSystemBuilder` に `coalchemical_hybrid_atoms` を足し、その環境原子だけ `fep_environment_mismatch` を免除して既存の **`fep_core` offset で線形補間**させる。新しい global parameter は足していないので、protocol / `run_fep` / `analyze_fep` は無変更。`validate_endpoints` には書き換え後の変異体 System を渡すので、**イオン端もそのまま端点検証される**。分子の幾何・制約・質量は水のまま（電荷も LJ も無い H がイオンに剛体で乗っているだけ）。
- **荷電かどうかの判定**: 残基名の表ではなく、merge する 2 つの端状態 System の `NonbondedForce` 部分電荷の総和の差（`coion.system_net_charge`）。プロトン化状態（ASP/ASH、HID/HIE/HIP、LYN）と力場がそのまま反映される。最初の実装はビルダーの報告値 `system_net_charge_e` を読んでいて、片方が `None` だと Δq = 0 扱いで黙って未補正になる穴があったので、System から直接測る形に直した（|Δq| ≤ 1e-3 e を中性とする）。
- **選び方（ツール側で閉じる）**: Δq = mut − wt、イオンは −sign(Δq) の 1 価、|Δq| 個（最大 2、非整数は拒否）。候補は部位重心から最小像距離で遠い順に、部位 ≥ 1.5 nm・溶質重原子 ≥ 1.0 nm・既存イオン ≥ 0.6 nm・選択済みの水 ≥ 1.0 nm を満たす最初のもの。イオンのパラメータは**系内の同符号 1 価イオンからコピー**（力場・水モデルと必ず整合、KCl でも動く、Na⁺/Cl⁻ を優先）。水 → イオンの自由エネルギーは両 leg でバルク同士なので相殺。
- **束縛**: 選んだ O を構築時座標に調和束縛（k = 1000 kJ/mol/nm²、`periodicdistance`、force group 4）。端点検証の後に System へ足す（構築座標でゼロ、比較対象の group 外）。全 λ で同一なので reduced potential の差には出ない。system.xml に入るので min / eq / fep が追加の配線なしで引き継ぐ。
- **拒否して未補正に落とさない**: `fep_coion_parameters_unavailable`（同符号イオンが箱に無い → 塩ありで solv を作り直す）、`fep_coion_box_too_small`（十分遠い水が無い → `--dist` を増やす）、`fep_coion_unsupported`（|Δq| > 2、非整数）。`--charge-correction none` は明示時のみで、相殺しない旨の警告を返す。真空系と中性変異は対象外（`method: none`、警告なし）。`estimate_ddg` の leg 照合に `charge_correction` を追加（キーの無い旧 manifest は `none` 扱い → 補正あり/なしの leg を混ぜると `fep_legs_incompatible`）。
- **test-run feedback 20（同日追記）**: `charge_correction` ブロックに co-ion が動く λ 区間 `lambda_range`（= `phase_bounds`、`fep_core` が動く相）と該当する `window_indices` を併記。`lambda_parameter: fep_core` だけでは区間を skill の説明から推論する必要があり、overlap 行列の落ち込みと突き合わせにくいという指摘。manifest の `charge_correction_detail` にも入る。
- **既知の設計上の選択**: イオンは core 相（p1..p2）で現れ、新側鎖の電荷は第 3 相で入るので、**中間 λ の箱電荷は 0 ではない**（A2D で λ=0.25/0.5/0.75 が −0.06/+0.37/+0.80 e）。自由エネルギー差は端状態の Hamiltonian だけで決まるので結果には効かない。各 λ で中性に保つには専用 parameter と窓ごとの値が要り、protocol の 5 parameter 契約を崩すので見送り。
- **実測（溶媒和 ACE-X-NME、amber14 + TIP3P、0.5 M、padding 1.7 nm、openmm ビルダー、CPU）**: A2D（Δq −1 → Na⁺、水は部位から 2.75 nm）端点差 A +6.5e-4 / B −5.1e-4 kJ/mol、K2A（→ Na⁺）+1.4e-4 / +2.8e-4、D2A（→ Cl⁻）pass。hybrid の正味電荷は λ=0, 1 とも 0.0000 e、`none` では B 端が +1。`run_fep`（CUDA、NPT、5 窓 × 4 ps）は全窓完走。**ddG への効果そのもの（補正あり/なしの差、箱サイズ依存の消失）はまだ測っていない** — A14D の再計算が最初の実測になる。
- **ついでに直した不具合**: `build_openmm_system` が溶媒和 PDB で `openmm_serialization_failed: object of type 'int' has no len()`。Pablo が溶媒の chain id を int で返し、`PDBFile.writeFile(keepIds=True)` が `len(chain.id)` で落ちる（residue id は既に str 化していたが chain は未対応）。`--endstate-builder openmm` の溶媒和経路はこれまで一度も実走していなかった。
- 引用 `Chen2018ChargeChangingFEP` を Crossref で確認して監査 .bib（116 keys / 115 DOIs）と packaged bib（21 件）に追加、topo の `metadata.fep.charge_correction == coalchemical_ion` で選択。guardrail 3 コード追加（409 codes）。`skills/md-fep`（SKILL / convergence）、tool-reference、`fep-references.md` §3・§8 を更新。
- **テスト**: `tests/test_fep_coion.py` 14 本（合成した箱での符号・パラメータ・最遠選択・2 個選択・KCl・4 種の拒否・束縛エネルギー、leg 照合、slow: 溶媒和 D2A の端点検証と両端の正味電荷、`none` の警告、塩なしの拒否）。

## 2026-09-20 — FEP test-run feedback 17–19: ラッパーのネイティブ振り分け、`explain_node` の blocking_codes と next（branch `feat/fep-ddg-node`）

3 件ともコードで確認でき、全部直した。

- **17. `bin/mdclaw` のツール名判定**: `TOOL="${1:-}"` なので、グローバルオプションが先行すると Slurm ツールがコンテナ内で走り `no Slurm client` になる。報告は `--output full list_tracked_jobs` だったが、**スキルが教えている `mdclaw --job-dir <jd> --node-id <id> submit_job …` の形も同じ経路**で、報告より範囲が広い。「`--` で始まらない最初の引数」では `--output full` の `full` を拾うので不十分。`_cli._detect_subcommand` と同じ規則（値を取るグローバルオプションの次のトークンを飛ばす、`--opt=value` は飛ばさない）を bash で実装し、`GLOBAL_VALUE_OPTIONS` が `_cli._GLOBAL_VALUE_OPTIONS` と一致することをテストで固定した。`--list` / `--version` / `--help` の分岐は従来どおり `$1` を見る。
- **18. `explain_node` の `blocking_codes`**: `validation.blocking_codes` は実行コンテキスト（親の状態・型・conditions）専用で、入力解決の拒否（`hybrid_topology_production_blocked`）はどこにも入らなかった。`validation` の意味は変えず、**トップレベルに `blocking_codes`**（validation の codes ∪ 入力解決の code、code が無い入力エラーは `input_resolution_blocked`）を足した。先頭キー順はテスト済みの契約（`success, code, ready_to_run, required_action`）なのでその後ろに置く。
- **19. `ready_to_run: false` なのに `next.action = run`**: `next` は CLI の envelope が `setdefault` で付ける汎用ステップで、pending ノードは常に `run`。`explain_node` 自身が「コンテキストは有効だが入力が組めない」とき `next = {action: blocked, blocking_codes, reason}` を返すようにした（親が未完了のケースは envelope が既に親のステップを返すので触らない）。`source_candidate_selection_required` も同じ経路で `blocked` になる（`required_action` が解決手段）。envelope の `next_step` 自体に入力解決を入れる案は、全ツールの出力ごとに resolver を回すことになるので見送り — `inspect_job` などの `next` は hybrid topo 下の pending prod に対して依然 `run` を出す（実行すれば pending のまま拒否される）。
- **テスト**: `test_bin_wrapper.py` に振り分け 6 ケース + オプション表の同期、`test_fep.py` の hybrid prod 拒否テストに `blocking_codes` と `next.action == "blocked"`。SIF overlay で `-k "explain or node or fep or cli or envelope or wrapper or …"` 712 passed / 2 skipped、ruff clean。`skills/common/tool-output.md` と tool-reference に `blocked` を追記。

## 2026-09-20 — FEP test-run feedback 11–16: 死んだ Slurm ジョブからの復旧、pending ノードの破棄、誤誘導 next_action、キャップ水素の誤警告（branch `feat/fep-ddg-node`）

テスト実行エージェントの指摘 6 件をコードで確認した。6 件とも事実で、うち 5 件を直し、16 は文書化のみ。

- **11a. pending のまま拒否されたノードに `trace_failure` を案内（`_common.finalize_error`）**: 原因は `fep_fragment_prep_required` 固有ではなく、`next_action` 未設定かつ node context があれば**ノード状態を見ずに** `trace_failure` を入れる共通処理。`node.json` の status を読み、`failed`（または読めない）ときだけ `trace_failure`、それ以外は guardrail の action を返すようにした。`extract_tripeptide` 側は具体的な `create_node --node-type prep --parent-node-ids …` を `next_action` に、破棄コマンドを `hints` に入れる。
- **11b / 15 の後半. 破棄できない pending ノード（`node/lifecycle.py`）**: `update_workflow_state --abandon [--reason]`。新しい status を足す案は `NODE_STATUSES` / `"failed"` の参照が 19 ファイルに及ぶので見送り、**`failed` + `failure_code = node_abandoned` で封印**する（`record_node_failure` 経由、failure artifact に reason）。`_ELIGIBLE_PARENT_STATUSES` に failed は無いので auto-parent と `parent_required` 候補から自動的に外れる。対象は「pending・`slurm_job_id` 無し・生きた子無し」のみ、それ以外は `node_abandon_refused`（葉から順に破棄する）。dag の `failed` 件数に混ざる点は既知の妥協。
- **15. `slurm_node_already_submitted` からの復旧（`slurm/node_sync.clear_slurm_submission_on_node`）**: ヒントが指す「clear stale metadata explicitly」の手段が実在しなかった。`update_workflow_state --clear-slurm-metadata` を追加: 非 terminal ノードの `slurm_*` metadata と submission intent を消して `pending` に戻し、`slurm_metadata_cleared` event に旧 job id を残す。二重投入ガードを保つため、squeue がその job をまだ載せている間は `slurm_job_still_active` で拒否（squeue が使えなければ warning 付きで通す）。ヒント文字列は実コマンドに差し替え。
- **14. accounting 無効サイトで queued 固定（`slurm/monitor.py`）**: `squeue -j <id>` は「終了済み」と「controller 不通」の両方で非 0 終了するので区別できない。`_slurm_job_in_queue` は `squeue -h -o "%i %F"` の**全件一覧が成功してその id が無い**ことを「確実に居ない」と判定する（pending array の `<parent>_[range]` は在りと見なす、失敗は `None`）。`check_job` が `slurm_status_unavailable` になる経路で、居ない **かつ** その job id を持つ非 terminal ノードがあるときだけ code を `slurm_job_vanished` にし、`stranded_nodes`・`stderr_tail`・`--clear-slurm-metadata` の `next_action` を返す。`list_tracked_jobs --sync` は握り潰していた結果を `stranded_jobs` / `warnings` に出す。**ノードは封印しない・tracker も書き換えない**: 最初の実装は tracker を `VANISHED` に上書きしたが、`test_slurm_status_fallback.py` の「不明時に過去の観測を書き換えない」契約（COMPLETED の記録を潰す）に反したので撤回した。
- **13. コンテナ未設定の `mdclaw` payload（`slurm/config.uncontained_mdclaw_warning`）**: cluster config は cwd ローカルなので新しい study dir では container 節が無い。submit_job / submit_array_job / submit_mps_job で「container 無し・`environment` 無し・payload が `mdclaw` を呼ぶ・**投入側の mdclaw 自身が image 内で動いている**（`SINGULARITY_CONTAINER` / `APPTAINER_CONTAINER`）」のとき `container_not_configured:` 警告。conda など native 環境からの投入は共有 FS で計算ノードからも見えるのが普通なので警告しない（ノイズ回避）。指摘どおり拒否にはしていない。
- **12. キャップ水素の誤警告（`structure/terminal_caps.py`）**: Modeller は不足分を足すので「追加 0 かつキャップに水素がある」は完備で到着しただけ（`extract_tripeptide` は調製済み断片を再 clean するので毎回これ）。警告は「追加 0 かつ水素が 1 つも無いキャップがある」ときだけに。
- **16. `.mdclaw_jobs.jsonl` が 3 箇所**: cwd / `output_dir` / `job_dir` への複製は意図した設計（どこからでも引ける）で、読みは重複排除、更新は全コピーに書く。挙動は変えず、`monitor-recover.md` と tool-reference に明記（1 ファイルにしたければ `MDCLAW_JOBS_FILE`）。
- **設計判断（同日ユーザー確認、4 件とも現状維持）**: (1) 破棄は `failed` + `node_abandoned` のまま。見送り: dag スナップショットに `abandoned` を別掲（誤読が実際に起きたら足す）、新 status `abandoned`（19 ファイル改修）、子なし pending を候補から除外（正当な chain 構築中の pending も消える）。(2) vanished は報告のみで封印しない。見送り: zombie として自動 failed（ノード作り直しが再発、squeue 一過性の誤判定が不可逆）、queued/running で分岐、`--sync --free-stranded` の一括解放（大量復旧が頻発したら足す）。(3) 警告は image 内からの投入時のみ。見送り: 常時警告（conda 運用でノイズ）、image 内では拒否（再発したら格上げ）。cluster config を親ディレクトリへ遡って探す案は根本原因に効くが解決規則の変更なので別件。(4) `--clear-slurm-metadata` は squeue に居れば拒否、squeue 不能なら warning 付きで通す。見送り: 不能時は拒否 + `--force`（反射的な `--force` で形骸化）、無確認、`--cancel` で scancel 同居。
- guardrail 4 コード追加（`node_abandoned`, `node_abandon_refused`, `slurm_job_still_active`, `slurm_job_vanished`）、golden 再生成（406 codes）。`skills/hpc-run/monitor-recover.md` に復旧・破棄・tracker の節、`skills/md-fep/SKILL.md` の `fep_fragment_prep_required` 行に破棄手順。
- **テスト**: `tests/test_stranded_node_recovery.py` 14 本（queue probe の array 判定、vanished の報告とノード非封印、still-active 拒否 → clear → progress/event、解放後は旧 job が stranded と言わない、abandon と候補除外、pending 拒否の next_action、image 内のみの警告）、`test_terminal_caps_for_pdb2pqr.py` に誤警告の回帰。SIF overlay で `-k "slurm or node or fep or …"` 949 passed / 2 skipped、ruff clean。CLI で source 直下に誤って作った prep ノードの `--abandon`（`failed` / `node_abandoned`）を実地確認。

## 2026-09-20 — FEP 設計レビュー残件 3 / 5 / 6: hybrid topo 上の prod 拒否、report 層の FEP 対応、leg ラベル・位相境界・拘束・端状態ビルダー（branch `feat/fep-ddg-node`）

前エントリの残件を一括で実装した。

- **3. hybrid topo 上の plain prod（`node/inputs.py`）**: `prod` の入力解決で topo 祖先に `fep_protocol` artifact があれば `input_resolution_code = hybrid_topology_production_blocked` を立てる。`explain_node` は `ready_to_run=False` でその code を `code` に出し、`run_production` / `run_sst2` は `input_resolution_code` を尊重して**ノードを pending のまま**拒否する（何も走っていないので `begin_node`/`fail_node` しない。他の解決失敗は従来どおり failed）。λ=0 の hybrid は WT の物理だが `topology.pdb` に WT 残基名のゴースト原子が混ざり、残基ベースの解析が全部ずれるため、警告ではなく拒否にした。`md-production` skill と CLAUDE/AGENTS に一言。
- **5. report 層（`evidence/reporting.py`, `evidence/citations.py`）**: `fep` を `prod` と同じ「sampling 段」として扱い（`sampling_node_ids` / `fep_node_ids`、replica 判定の `production_frontier` は sampling で計算、`production_node_ids` は prod のみのまま）、subject ごとに `alchemical` ブロック（hybrid topo の `fep` metadata、fep ノードの窓・拘束、`fep_mbar` leg の dG、`fep_ddg` の ddG。metadata のみ、artifact は再読しない）。`_SETTINGS` に `fep` / `lambda_indices` / `analysis` / `cycle` などを追加して `comparison` に載せた。引用: `Shirts2008MBAR`・`Chodera2007Timeseries`・`Chodera2016Equilibration` は監査済みライブラリから packaged bib にコピー、`Beutler1994SoftCore`・`Gapsys2015pmx`・`Seeliger2010Thermostability`・`Klimovich2015Guidelines` の 4 件は Crossref API で書誌を確認して監査 .bib と packaged bib の両方に追加（監査 .md に同日の addendum、115 keys / 114 DOIs）。選択は全て node metadata（topo の `fep`、analyze の `analysis`/`subsampled`、prep の `leg_role`、ddG の `cycle`）に鍵付け。`test_evidence_server.py` の packaged 件数ピンを 13 → 20 に。
- **6a. leg ラベル一般化（`fep/analysis.py`）**: `estimate_ddg --cycle folding|binding`。`DDG_CYCLES` 表に leg 名（folded/unfolded、complex/apo）・量・符号規約。役割判定は「派生 leg の prep に `leg_role`（= comparison leg 名）」→ 無ければ `analysis_subjects`（cycle の leg 名、親順）→ `fep_leg_role_ambiguous`。report / metadata / receipt に `cycle`。apo leg を派生させる prep ツールは未着手（`fep-references.md` §8 に明記）。
- **6b. `--phase-bounds p1,p2`（`fep/protocol.py`, `fep/build.py`）**: `parse_phase_bounds`（0 < p1 < p2 < 1）、`lambda_to_parameters(lam, bounds)`、`windows_from_schedule(..., phase_bounds=)`、`build_protocol(phase_bounds=)`、`load_protocol` が既定を埋める。manifest / result / node metadata に記録。`protocols_equivalent` は窓ごとのパラメータを比べるので境界違いは非互換として出る。
- **6c. `run_fep --restraint-atoms {solute_heavy,CA,backbone,heavy} --restraint-force-constant`（`fep/run.py`）**: `simulation/restraints.select_restraint_atoms` を再利用、基準座標は eq の state（無ければ topology state）、`CustomExternalForce` を窓ごとの System に追加（非周期系は `sqrt(...)` 距離）。leg 内で単一の Hamiltonian にするため、拡張は親索引の `restraint` を継承し、異なる指定は `fep_windows_incompatible`。`fep_windows.json` / `fep_result.json` / ddg.json の legs に記録、`collect_windows` は拘束の異なる索引の pooling を拒否。`_resolve_fep_inputs` に `chain_identity_map_file`（`solute_heavy` 用）。
- **6d. `--endstate-builder openmm --forcefield-xml …`（`fep/build.py`）**: `_build_endstates` が `build_openmm_system` を呼ぶ。`build_openmm_system` は箱を PDB から読むが `solvate_structure` の出力に CRYST1 が無いので、`box_dimensions` に `build_amber_system` と同じ 2 Å の余白を足した CRYST1 を書いた PDB を渡す（`_pdb_with_cryst1`）。prepared ligand はこの経路では未対応（`fep_endstate_build_failed`）。manifest の `forcefield` は XML リスト。
- **テスト**: `test_fep.py` 66 本（+ `TestBuildHybridSystemDirect`: 真空 ACE-LEU-NME → ACE-ALA-NME を amber / openmm 両ビルダーで通し、端点検証 pass、`phase_bounds=0.2,0.8` が protocol に載る; 拘束の継承・拒否・記録・pooling 拒否; hybrid eq 下の prod が resolver / explain / run_production で拒否され pending; binding cycle の subjects 経由の役割決定; `parse_phase_bounds` の境界）、`test_evidence_server.py` に alchemical 系譜の report と引用、legs/ddG の replica 判定。

## 2026-09-20 — ddG をノードにする: 2 leg を 1 job に、`estimate_ddg` を comparison analyze に（branch `feat/fep-ddg-node`）

設計レビュー（同日）で最大の弱点とした「ddG が DAG のどこにも無い」件の対応。案は 2 つあり、job をまたぐ親参照（cross-job comparison）は `read_node` の同 job 前提が `node/*.py`・`progress.json`・lock・events・snapshot・`reporting.py` の lineage に貫かれていて触る範囲が広い。代わりに **unfolded leg を folded leg の `prep` から派生した `prep` 子ノードにする**と、ddG は「2 つの analyze 親を持つ `comparison` analyze」という既存の形にそのまま載る（`_ALLOWED_PARENT_TYPES["prep"]` は prep → prep を許可済みで `create_mutated_structure` が前例、`n_parents >= 2 and all analyze` の resolver 分岐も既にあった）。これを実装した。

- `extract_tripeptide` を `@node_tool(node_type="prep")` に。親 prep の `merged_pdb` から断片を切り（`cut_fragment`、chain・残基番号・プロトン化変異名を保持）、`clean_protein(cap_termini=True, preserve_input_protonation=True, protonation_method="no-prediction")` でキャップとキャップ水素を付け、`merged_pdb` / `chain_identity_map` / `disulfide_bonds` / `fragment_pdb` を artifact に、metadata に `leg_role = "unfolded"`・`unfolded_model`・`derived_from_prep_node_id` を書く。`merge_structures` は chain を A から振り直すので使わず、1 成分の `chain_identity_map` を自前で書く（`--mutation B:…` の leg で chain が変わる事故を防ぐ）。Trp-cage の実データで dev run の unfolded prep（ACE4–GLN5–TRP6–LEU7–NME8、72 原子）を byte 単位でなく残基列・原子数で再現。以前の dev run は `prepare_complex --cap-termini --ph 7.4`（propka）で tripeptide のプロトン化を再予測していたが、新ツールは親のプロトン化をそのまま使う（両 leg で同じ化学種にするため。`--protonation-method propka` で旧挙動）。
- `estimate_ddg` を `@node_tool(node_type="analyze")` に。node mode は `analysis_data_scope: comparison` の analyze ノードで、親は 2 つの `analyze_fep`（metadata `analysis == "fep_mbar"` と `fep_result` artifact を要求、順不同）。leg の役割は各親の祖先 prep の `leg_role` から決め（無ければ `analysis_subjects` の親順、それも無ければ `fep_leg_role_ambiguous`）、差を取る前に変異・λ プロトコル（`protocols_equivalent`）・力場・水・HMR・T・P を照合して不一致は `fep_legs_incompatible`（ノードは pending のまま）。`artifacts/ddg.json` と metadata `analysis = "fep_ddg"` に記録、job params に `study_dir` があれば study log にも decision を残す。direct（Python）mode は `--folded/--unfolded` で維持（CLI は他の stage tool と同じく DAG-only）。
- `comparison` scope の `comparison_mapping` と `analysis_subjects` を create 時任意に（mapping を出す場合は subjects 必須）。今 comparison を消費するツールはゼロなので影響なし。resolver の `branches_input` に `analyze_node_id` / `analysis` / `fep_result_file` を追加。
- envelope `next`: 完了した `analyze_fep` に対して、相手 leg が完了していれば「comparison ノードを作って `estimate_ddg`」（親順は folded, unfolded に正規化）、pending の ddG 子があればそれを run、folded しか無ければ「`prep_001` の下に prep を作って `extract_tripeptide --mutation <label>`」、ddG 完了で done。pending の comparison ノードは `estimate_ddg` を先頭に。
- guardrail 6 コード追加（`fep_fragment_prep_required`, `fep_tripeptide_cap_failed`, `fep_ddg_scope_invalid`, `fep_ddg_parents_invalid`, `fep_leg_role_ambiguous`, `fep_legs_incompatible`）、golden 再生成（401 codes / 85 tools）。
- skill `md-fep` を 1 job 構成に書き換え（bootstrap 1 回、`fetch_structure --source local` と `--plan` の 2 job 宣言が消え、step 6 は `create_node --node-type prep --parent-node-ids <prep_001>` → `extract_tripeptide`、step 7 は comparison ノード）。`tool-reference` / `architecture`（mermaid 追加）/ `analysis-node-contract` / CLAUDE・AGENTS / `fep-references` §2・§6 を更新。
- テスト: `tests/test_fep.py` 57 本（`TestDdgNode` 5 本: 逆順親でも役割が DAG から決まる、非互換 leg は pending、役割不明と subjects fallback、scope/親検査、envelope の 3 状態; `TestUnfoldedLegPrep`（slow）2 本: 6KUY 97–107 断片を PDBFixer で整えた「prepared.pdb」から A:W99A を切って `clean_protein` でキャップ、番号 98–100 と chain A を保持、prep ノード下で solv resolver が断片を拾う）。`test_node.py` の comparison テストを「bare scope 可 / mapping には subjects 必須」に置き換え。

設計レビューの優先順位 2（study 型 + study レベル next + leg 間条件照合）は、この形で大半が不要になった: 1 job なので study レベル `next` の穴が無く、条件照合は ddG ノードが行う。残るのは 3（hybrid topo 上の plain prod への警告）、5（report 層の FEP 対応: `production_node_ids` が fep を拾わない、Methods 文）、6。

## 2026-09-20 — hybrid-topology FEP の実測検証: 1MEL VHH 5 変異を NAMD/CHARMM36 参照と比較

`skills/md-fep` を最初から最後まで通して、既存の NAMD+CHARMM36 の ddG ラベル
(`~/tmp/sim2real/data/source_labels/fep/`, 生データ `/data/{odas,kazu}/vhh_fep/`)
を再現できるかを実測した。スタディは `/home/yasu/tmp/fep_bench_vhh_1mel`、
結果の詳細は同ディレクトリの `RESULTS.md`。

**構成**: 1MEL author chain B (VHH 単体, 残基 2-133) を共有 `prep`/`solv` から
5 本の `topo` に分岐。ff19SB + OPC, 0.15 M NaCl, 立方体 76.74 Å (62,333 atoms),
HMR 4 fs, NPT 300 K/1 bar, 21 窓 × 2 ns/leg, MBAR。unfolded は各変異ごとに
`extract_tripeptide` で切り出した ACE-X-X-X-NME (5,062-6,463 atoms)。
合計サンプリング 420 ns、SLURM 28 ジョブ、失敗ゼロ、A6000 3 枚で実時間 12.5 h。
`build_hybrid_system` 10 件すべて endpoint validation 合格 (|ΔE| ≤ 0.007 kJ/mol)、
`analyze_fep` 10 件すべて警告なし (overlap 0.054-0.115, leg 誤差 0.24-0.46 kJ/mol)。

**結果 (kcal/mol, 正 = 不安定化)**

| 変異 | MDClaw | 参照 | 差 |
|---|---:|---:|---:|
| L113A | +6.462 ± 0.108 | +6.206 | +0.256 |
| V48A  | +3.863 ± 0.089 | +4.198 | -0.335 |
| S105A | +0.083 ± 0.108 | +0.018 | +0.065 |
| V64A  | -2.262 ± 0.113 | -2.553 | +0.291 |
| H111A | +0.559 ± 0.154 | -6.629 | **+7.188** |

H111A を除く 4 点で **Pearson r = 0.9975, MAE = 0.237, RMSE = 0.258, 符号一致 4/4**。
力場・unfolded モデル・alchemical 経路・ジスルフィドがすべて違うことを考えると、
2 手法を区別できる精度の下限に達している。

**実装が熱力学的に正しいことの直接証拠**: MDClaw の hybrid 経路 (core が CB を保持、
3 phase、soft-core) と NAMD の dual topology (CB から丸ごと annihilate) は *leg* の
値が大きく違うが、その差は残基種ごとの定数で ddG では相殺する。folded 側オフセット
(MDClaw folded − `scan.dat`) と unfolded 側オフセット (MDClaw tripeptide −
`dG_unfold[wt]`) を比べると Leu 8.961/8.705、Val 19.551/19.886 と 20.021/19.730、
Ser 13.099/13.034 と 0.06-0.34 kcal/mol で一致する。この不一致量がそのまま ddG 誤差。
同一変換 (Val→Ala、写像も core 7 / 消失 9 / 出現 3 で一致) の V48A と V64A は
unfolded leg が 0.156 kcal/mol 以内で一致し、参照が主張する 6.751 kcal/mol の差を
6.125 で再現した。L113A は 1 ns → 2 ns 延長で +6.323 ± 0.148 → +6.462 ± 0.108
(ドリフト 0.139 < 合成誤差 0.183) で time-forward にも整合。

**H111A のずれは MDClaw ではなく参照ラベル側の欠陥**: His だけ folded/unfolded の
オフセットが -2.487 と -9.674 で 7.19 ずれる。参照の unfolded leg は計算ではなく
`/data/share/ddG.jl` の固定表 (`dG_unfold[H] = 29.0`) で、folded 側は HSD を使って
いるのに、参照データ内に存在する capped peptide の X→Ala 実測は HSE 36.83 と
HSP 22.45 のみで **HSD が無い**。29.0 はどちらとも一致せず両者の平均 (29.64) に近い。
参照自身を自己整合させるには `dG_unfold[H] ≈ 21.84` が必要で、使われている値は
7.2 kcal/mol 高い。影響するのは 844 ラベル中 4 行 (`1mel` H110A/D/Q/I、4idl には
His-WT 行なし) だが、それらは 1mel で最も安定化側のラベル 2-5 位 (-6.6 〜 -7.9) を
占めるので、1mel の安定化テールは実質この 1 残基の疑わしい参照値に支配されている。

**参照プロトコル側のもう一つの問題**: `/data/odas/vhh_fep/1mel/3_psfgen/build.tcl` に
`patch DISU` が無く、VHH の S-S 2 本が未結合のまま計算されている (平衡化後の SG-SG は
C22-C96 3.76 Å, C33-C109 4.58 Å)。今回の MDClaw 側は 2 本とも形成している。
選んだ 5 残基はいずれも Cys SG から 7 Å 以上離れており、上の一致度から見て
少なくともこれらの位置では効いていない。

---

## 2026-09-19 — FEP テスト計算エージェントからのフィードバック 7 件: 検証のうえ全件採用

別エージェントが T4L/1MEL 系で Phase 1 を回した際の報告。各項目をコードと実データで裏取りしてから直した（主張どおりでなかった箇所は明記）。

1. **`inspect_cluster` が異種 GPU パーティションを最後のノード行で上書き** — 事実。`_parse_sinfo_text` も JSON 経路も `gpu_type` / `gpus_per_node` をノード行ごとに代入していた。両経路をノード行 → `_aggregate_partitions` に統一: `gpu_type` は 1 種類のときだけ、`gpu_types` / `gpu_inventory`（モデルごとの nodes・gpus_per_node・node_list）/ `node_gres`（生 GRES + GresUsed）を追加、`gpus_per_node` は最大値、混在パーティションは warning。JSON 経路は現代の `gres: {total, used}` dict と `nodes.nodes[]` も読むようにした（以前は文字列前提で GPU を拾えなかった）。フォールバック warning は**既に出ていた**が、sinfo の stderr（`serializer/json`）を添えるようにした。GresUsed は `sinfo -N -h -O NodeList,GresUsed` を best-effort で追加取得。報告のクラスタ構成（floyd a6000×7 / m1 / n2,n4 1080×10 / n5 2080×9、JSON 無し）をテストで再現し、`gpu_type=None`, `gpu_types=[1080,2080,a6000,rtx8000]`, inventory, total_gpus=38 を固定。
2. **ホスト python で slurm ツールを走らせると「X not found in PATH」が約 40 行** — 事実。`BaseToolWrapper` を module レベルで 8+13+13 個作っており import ごとに warning。`_common.py` にモジュール集合を置いてツール名ごとに 1 回だけ warn。
3. **`bootstrap_md_workflow` の既定 `workflow_steps` が prod 固定、かつ `--plan` で複数 job を宣言しても最初の job 分しか生成されない** — 事実（後者は報告に無かったが同じ穴）。`sampling_stage: prod|fep` を追加し、宣言された全 job について既定 steps を生成。`md-fep` SKILL step 1 に `--sampling-stage fep`。
4. **`External-bond patcher could not pair … VAL#2.N, SER#133.C` がキャップ無し鎖末端で必ず出る** — 事実で、FEP に限らず `build_amber_system` 全般の誤検知（Trp-cage folded の amber_metadata にも `ASN#1.N, SER#20.C` で出ていた）。候補列挙が残基名で mid-chain テンプレートを引くため末端の N/C が「相手待ち」になる。鎖の先頭残基の N / 末尾残基の C（蛋白質残基のみ）を候補から外した。Trp-cage WT を再ビルドして `unpaired_external_atom_count` 2 → 0、A6W tripeptide の直接ビルドでも warning 無しを確認。
5. **`submit_array_job` の結果に `slurm_job_id` が無い** — 事実（`parent_job_id` にはあった）。`slurm_job_id = parent_id` も返すようにし、`--dependency afterok:<slurm_job_id>` が single/array で同じ欄で組める。
6. **SKILL step 1 と step 6 のパス表現の二重化** — 表現を統一（artifact キー `merged_pdb` と実パスを同じ文で併記）。
7. **`build_hybrid_system` が端点検証で無断で GPU を掴む** — 事実（`fastest_platform_name()` 固定）。`--platform` / `--device-index` を追加（`resolve_platform_name`）し relax / validate / state 書き出しに通した。a6w tripeptide（5k 原子）を `--platform CPU` で直接ビルド: 40 s、端点差 −0.001 / −0.003 kJ/mol。

採用しなかったもの: なし。テスト: slurm / study / fep / disulfide / registry / cli / guardrail 383 本 pass、tests/ 全体（slow 除外）pass、ruff clean。

**追記（同日 17:00）— 追加フィードバック 3 件、いずれも事実。** (8) `--parent-node-ids a,b,c` は CLI の `nargs='+'` で 1 トークンになり `referenced_node_missing`。`md-fep` SKILL step 5 が `[,<fep_id2>,...]` と書いていたので skill の指示どおりで必ず踏む。`create_node` で `parent_node_ids` / `dependency_node_ids` をカンマ・空白で分割するようにし（ノード id はどちらも含まない）、skill 表記を `<fep_id> [<fep_id2> ...]` に直した。実 CLI で `fep_003,fep_005` → 親 2 つを確認。(9) `bootstrap_md_workflow` の「plan に無い job」拒否は `code` 無しで envelope が `unhandled_error` を付けていた。`job_not_in_study_plan` + `planned_job_ids` + `record_study_plan --overwrite true` を示す `next_action` に。(10) `record_study_plan` の `plan` は record（`plan.plan.jobs` と二重）で、失敗時・`overwrite=False` 時は `None`。成功時に flat な `job_ids` を追加（`get_study_plan` も）、既存 plan の拒否に `study_plan_exists` コード。guardrail 395 codes。

## 2026-09-19 — FEP 干渉レビュー: hybrid `topology.pdb` の残基名正規化（pinned guard 赤）と SST2 の同根の穴

FEP 以外への干渉を調べたレビューで、`tests/test_pdb_export_resname_guard.py::test_pdb_writefile_inventory_is_pinned` が赤になる 1 件が出た。根は `hybrid_topology()` が `PDBFile(wt.topology.pdb).topology` の残基名をコピーすること: ローダーが HIE/CYX/ASH/GLH/LYN/WAT → HIS/CYS/ASP/GLU/LYS/HOH に正規化するので、`build_amber_system` の `topology.pdb` が復元している変異名が hybrid の `topology.pdb` では失われる（Trp-cage の開発 run では変異名残基が無く、水は溶媒和段で既に HOH だったため見えなかった）。物理は `system.xml` なので無関係、影響は md-report の Methods（プロトン化状態）・resname 選択・deposit・可視化。

**レビューの提案（`restore_resnames_by_residue_key` で書き戻し後にテキスト復元）は実際には効かない。** 実データで確認: 溶媒和済み `topology.pdb` は水を蛋白質と同じ chain A・残基番号 1.. で振るため、W6A folded の 20 蛋白質残基キーは全部が水と衝突（a6w_smoke も 5/5）。キー復元は曖昧キーを検出すると全体を拒否して `None` を返すので、何も復元されずに終わる。代わりに **`restore_topology_resnames_from_pdb(topology, source_pdb)`** を `structure/pdb_utils.py` に追加した: 元 PDB の ATOM/HETATM 残基名を原子順で読み、ロード済み Topology オブジェクトの残基名に戻す（`PDBFile` は原子順と個数を保つので正確; 個数不一致なら `None` で何も変えない）。`_assemble_hybrid` で両端状態の Topology にかけてから mapping / hybrid 派生をするので、hybrid `topology.pdb` も manifest の `old_name`/`new_name` も変異名を保つ。guard の `RESTORE_HELPERS` に追加、`"fep/build.py": (1, "restore")` を pin。テスト: `test_restore_topology_resnames_puts_source_names_on_the_loaded_object`（水が蛋白質と同キーの例で HIE/CYX/WAT を戻す、再適用は no-op、個数不一致は None）、`TestVacuumPipeline::test_hybrid_topology_pdb_keeps_amber_variant_names`（真空 HIE→ALA を `_assemble_hybrid` → `_write_artifacts` に通し、hybrid PDB が HIE で HIS を含まないこと、HETATM 記録数が hybrid 残基の原子数と一致すること、再ロードで粒子数が System と一致すること）。

同じ guard は origin/main の時点で既に赤だった: `simulation/tempering.py`（SST2、`7ee4be8`）が `final_structure.pdb` を `PDBFile` ロード済み topology から直接 `writeFile` していて未登録・復元なし。`run_production` と同じ `render_simulation_pdb_preserving_resnames`（名前復元 + 最終 state の box + 周期系ならイメージング）に置き換えた。これで guard は green。

小さい 2 件: `skills/md-analyze/SKILL.md` の scope 列挙に `alchemical` の存在を注記（md-fep の `analyze_fep` 用であり md-analyze は使わない）。`NODE_TYPE_ALIASES` から汎用語 `window` / `lambda` を外し `alchemical` / `fep_window` のみに（`create_node --node-type window` が黙って fep になるのを防ぐ）。

テスト: tests/ 全体（slow 除外）2274 pass / 22 skipped、fep + node + envelope 282 pass、ruff clean。

## 2026-09-19 — FEP 再レビュー対応（N1–N9）: 親+子の二重計上と、回収／部分延長で窓が落ちる件

`a8b3987` の再レビューで新規に挙がった 9 件に対応。Medium の 2 件はいずれも索引の扱い。

- **N1（親 fep と延長子を両方 analyze の親にすると segment が二重計上）**: `collect_windows` が解決後の `energies_file`（realpath）をキーに窓ごとに重複を落とし、`"N segment(s) were listed by more than one parent index … counted once"` を warning に出す。レビューの調和振動子トイ（葉のみ 800 → 親+子 1200 で誤差が 0.0369 → 0.0302 に縮む）を `test_parent_and_child_indexes_are_not_double_counted` で固定（親+子でも [800]×5、warning あり）。真空 e2e でも親+子 → [40, 40, 20] を確認。
- **N2（回収／部分延長で再サンプルしない親窓が子の索引から落ちる）**: `run_fep` は `parent_windows` にあって `indices` に無い窓の record を segment そのままで索引に載せる（`carried_over_windows` を索引・result・metadata に記録、部分書き出しにも最初から含める）。回収は `--lambda-indices 7-20` だけで済み、延長を絞っても葉が常に全窓を持つ。真空 e2e: 0–1 済みの索引に `--lambda-indices 2` → 索引 [0,1,2]、0/1 は 1 segment のまま、`analyze_fep` [20,20,20]。`windows.md` の回収例を `--lambda-indices 7-20` に変更（`all` は済んだ窓ももう 1 回回す旨を明記）。
- N3: 延長／回収時に温度も親索引と照合（`fep_windows_incompatible`、テスト追加）。N4: `per_segment[].n_after_discard` を segment 自身の数に（テストで [400, 400] を確認）。N5: `extended_from` と各 record の `restarted_from` も索引ディレクトリ相対に（`load_windows_index` で解決）。N6: `run_fep` の入力解決失敗を `analyze_fep` と同じく pending 維持に揃えた（何も走っていないので同じノードを再実行できる。`run_production` は begin_node + fail_node で terminal 化する前例だが、そちらは据え置き）。N7: eq から始める窓で `equilibration_time_ns < 0.05` なら warning（開始最小化で箱全体の熱運動が消える: A6W λ=0 窓で −6.72×10⁴ → −7.76×10⁴ kJ/mol）。`windows.md` に「fresh 窓は ≥ 0.1 ns、継続窓は 0」を追記。smoke で使った 0.02 ns は短かった。N8: `_BondedSpec.term`（"Bond"/"Angle"/"Torsion"）を明示し文字列スライスを廃止。N9: `_is_alchemical` を job 全体の `fep_mutation` 判定から「ノードの topo 祖先に `fep_protocol` artifact があるか」のノード単位判定に（通常 topo と hybrid topo が同居する job で通常枝の eq が prod のまま）。

テスト: fep 47 本（slow 含む）、既定セット + 関連 666 本 pass、ruff clean。

**追記（同日 14:10）— 三巡目の小さな指摘 6 件。** (1) 索引をループ前にも `complete: false` で書き、最初の窓で落ちても carried-over 窓を含む部分索引が必ずディスクにある（失敗 result の `fep_windows` が存在しないファイルを指さない）。(2) `state.xml` の要求は継続する窓（`indices` に含まれる親窓）だけに限定; carry-over 窓は `energies.npz` だけでよい（エラー文に「`--lambda-indices` から外せば carry over される」と案内）。(3) 失敗 result の `completed_windows` を `indexed_windows`（索引が列挙する全窓）に改名、`sampled_windows`（このノードが完了した窓）と区別を docstring に明記。(4) `window.json` も自身のディレクトリ相対に（`energies.npz` / `state.xml` / `restarted_from`）; 「索引に絶対パスなし」を「artifacts に絶対パスなし」に広げた。(5) `_summary_fep` に `(+N carried over)`。(6) `tests/test_envelope.py::test_alchemical_routing_is_decided_per_branch`: 同じ solv の下に plain topo 枝と hybrid topo 枝（`fep_protocol` artifact あり）を作り、job params に `fep_mutation` を入れた状態で、plain eq → prod / `run_production`、hybrid eq → fep / `run_fep` + batch_command、pending fep → run `run_fep`、completed fep → analyze + `alchemical` conditions を固定。真空 e2e にも「最初の窓で crash → 部分索引 [0, 1]」「state.xml 無しの窓は継続不可・carry-over 可」「window.json 相対」を追加、receipt テストに fep ケース追加。既定セット + 関連 668 本 pass。

## 2026-09-19 — FEP レビュー対応: 窓開始配置・部分失敗の回収・相対パス・bonded 三重複の統合（branch `feat/fep-hybrid-topology`）

前エントリの実装に対するレビュー（A: 実運用前、B: 契約・頑健性、C: 簡素化、D: 細部、E: テスト）を一括で対応した。最初の実装は `0100a38` としてコミット済み。

**A1/A2（small→large の窓開始配置と端状態ビルド）— 予測どおり顕在化し、対策が効いた。** 溶媒和済み W6A tripeptide（`unfolded/topo_002/artifacts/mutant_input.pdb`）から `extract_tripeptide --mutation A:A6W` で ALA 中心の断片を切り、`jobs/a6w_smoke` として prep → solv（`--dist 10`、3,875 原子）→ `build_hybrid_system --mutation A:A6W`（HPacker、端点差 0.064 / 0.053 kJ/mol、appearing 15 原子、mutant 端状態ビルドは通る = A2 は問題なし）→ min → eq → `run_fep` 21 窓 × (0.02 + 0.05 ns) → `analyze_fep`。**λ ≥ 0.75 の窓は eq 状態のエネルギーが +2.4×10⁸ kJ/mol**（水が TRP 環の位置に入り込んだまま hard LJ が入る）。`run_fep` に入れた「窓の λ で `LocalEnergyMinimizer` 200 反復 → 速度再設定 → 平衡化」で全窓 −77,200 kJ/mol 前後に落ち、21 窓とも NaN 再試行なしで完走（97.9 ns/day）。λ ≤ 0.7 は soft-core が効いて開始エネルギー −6.1〜−6.7×10⁴ kJ/mol と穏やか。dG_A6W(tripeptide) = +1.09 ± 2.43 kJ/mol（窓 0.05 ns、独立サンプル 10–45/窓で誤差は当てにならない）; 順方向 W6A の −7.37 ± 1.47 と符号は整合、大きさは 2σ 差で、開始構造（HPacker の TRP 回転異性体 vs 結晶）とサンプリング長を考えると往復一致の検証にはならない。往復一致は窓 ≥ 1 ns で別途。

**A3（部分失敗の回収）**: `fep_windows.json` を窓ごとに書き直す（`"complete": false` → 最後に true）。失敗ノードは親になれないので、`run_fep --restart-windows-file <failed>/artifacts/fep_windows.json` を追加: 索引にある窓はその state から継続（segment 連結）、無い窓は eq から開始。`windows.md` に「1 ノード = 窓サブセット」を 1 ns/窓超の既定として明記、guardrail 文も実装に合わせた。真空 e2e テストで部分索引 → 回復 → 解析（[40, 40, 20] サンプル）を固定。

**A4（絶対パス）**: 索引スキーマ v2。`state_file` / `energies_file` / segment / protocol / XML は索引ファイルのディレクトリ相対（`os.path.relpath`）、親鎖のコピー時に再相対化。`load_windows_index` が解決し、v1 の絶対パスも素通しで読める。プロトコル一致は文字列比較でなく `protocols_equivalent`（λ 列 + 5 成分 + 変異ラベル）。テストで job ディレクトリを移動して再解析し同じ dG を確認。

**B**: eq→次段の `next` が prod を指していた件（B1）は `_envelope` に表 `_ALCHEMICAL_FORWARD = {"eq": "fep"}` / `_ALCHEMICAL_PREFERENCE`（progress params の `fep_mutation` で判定）を置き、analyze の create_command に `--conditions '{"analysis_data_scope": "alchemical"}'` を付ける。`--workflow` は `eq > {prod | fep} > analyze`。B2: pressure 既定は eq の ensemble に従う（NVT eq なら NVT、不明なら警告付き 1 bar、真空は常に NVT）。B3: 親索引が読めない／親集合外の `--lambda-indices`／親と異なる `--pressure-bar` はすべてエラー（`fep_windows_missing` / `fep_lambda_index_invalid` / `fep_windows_incompatible`）。B4: `parse_lambda_indices` は `ProtocolError(fep_lambda_index_invalid)`、`sampling_time_ns` 等は `begin_node` 前に `invalid_parameter_value`（ノードは pending のまま）、`parse_mutation_specs` は `MutationSpecError(code=...)` を投げるようにして文字列照合を消した。B5: `windows_from_schedule` は 0→1 の厳密増加スカラー列のみ（dict 形は削除 = C5）、`collect_windows` は温度に加え pressure/ensemble を照合。B6: segment 単位で discard → subsample → pool（`per_window[k].segments` に内訳）。B7: `estimate_ddg` の既定出力は `<study>/evidence/ddg_<mut>.json`、無ければ `outputs/`（完了ノードの artifacts には書かない）。B8: `ANALYSIS_DATA_SCOPES` に `alchemical` を追加、fep 親には必須・他では拒否。B9: `asymmetric_core_exceptions > 0` を hybrid_report の warning に。B10/B11: 分散補正の欠落と hybrid 残基の CONECT を `fep-references.md` §3 と docstring に既知の近似・制限として明記。

**C**: `_add_bonds/_angles/_torsions` を `_BondedSpec` 表 + `_add_bonded` 1 本に（hybrid.py −110 行、端点テスト 5 変異で回帰確認）。exception 分類は `dummy_switch` 表で 1 ブロック、`_CmapSink` クラスは dict + 関数に。`build_hybrid_system` を `_resolve_inputs / _build_endstates / _assemble_hybrid / _write_artifacts` に分割し、`hybrid_manifest.json` を唯一の正（`mapping_summary`, `statistics` もここ）にして node metadata は `fep.{mutation, n_windows, endpoint_validation_passed}` に絞った。`result["mutation"]` は文字列のまま、dict は `mutation_spec`。`find_residue_index` 削除（`build.locate_residue_index` に統一）、`old_by_name_index` → `_record_at`。core 結合不一致の自己修復は `fep_mapping_failed` で停止。probe の System 逆シリアライズは XML テキストの `name="fep_*"` 検査に。`resolve_platform_name` を `simulation/_base.py` に追加（fep のみ使用）。`fail_tool` を `node/lifecycle.py` に置き、fep 3 ツールの失敗ブロックを統一。

**D**: `mutant_model/` は残す。`extract_tripeptide` の空 chain ID を `" "` に正規化。DCDReporter の private close は削除。`run_with_halved_timestep` を窓ごとに適用。`CORE_CANDIDATE_NAMES` に `HB`（VAL→ILE で端点差 < tol を確認）。`--sample-interval-ps` のコスト注記。延長例に `--equilibration-time-ns 0`。tripeptide の `--dist 10`。

**テスト**: `TestHybridVacuum` を LEU→ALA / GLY→PRO / LEU→PHE / ALA→TRP / VAL→ILE に拡張、`TestVacuumPipeline`（真空 3 窓 e2e: 引数検証コード、相対パス、部分索引回復、segment 連結、移動後再解析、pressure 不一致拒否）を追加。既定セット + fep 関連 665 本 pass、ruff clean、guardrail golden 再生成（393 codes）。

**未着手**: 実測 ddG があり変異体も折り畳む系での定量検証（T4 lysozyme L99A など、窓 ≥ 5 ns、複数 replica）、往復（W6A ↔ A6W）一致の確認、非平衡スイッチング、電荷変化の有限サイズ補正、結合親和性 leg。

## 2026-09-19 — 1 点変異 ddG の hybrid-topology FEP を `mdclaw/fep/` に実装（`0100a38`）

外部 FEP フレームワークに依存せず、OpenMM 標準 API だけで hybrid System を組む `fep` サーバーを追加した。新ノード型は `fep` 1 つ（親 `eq` / `fep`、子 `fep` / `analyze`）、hybrid 構築は `topo` の一種 `build_hybrid_system` として既存スパインに挿す。ツール 5 本: `build_hybrid_system`（topo）、`run_fep`（fep）、`analyze_fep`（analyze）、`extract_tripeptide` / `estimate_ddg`（ノード無しヘルパー）。設計と参考実装（pmx / Perses / OpenFE / GROMACS / GENESIS）の比較は `docs/research/fep-references.md`、契約は `tool-reference.md` の `fep/` 節、手順は `skills/md-fep/`（SKILL + `windows.md` + `convergence.md`）。

要点: core は backbone + CB のみ、側鎖はダミー。ダミー bonded は全 λ で on（両 leg で相殺）、ダミー非結合は電荷線形 + Beutler soft-core（α=0.5）を (ダミー × rest) interaction group で、old と new は互いに不可視。core の差は `Custom*Force` で `(1−λ)E_A + λE_B`、ff19SB CMAP は `CustomCVForce` で 2 つの `CMAPTorsionForce` を混合。5 global parameter を λ∈[0,1] の区分線形（境界 0.25 / 0.75: 旧電荷 off → 立体 swap → 新電荷 on）で駆動、既定 21 窓。変異体側鎖は HPacker（PDBFixer フォールバック）で作り、変異残基だけを WT PDB にテキスト splice（他は byte-identical、CONECT 再マップ）。構築後に appearing 原子だけ状態 B で最小化（GLY→PRO で 8×10⁴ kJ/mol の歪みが出たため）、λ=0/1 で元 System との差 ≤ 1 kJ/mol を検証して失敗なら `fep_endpoint_validation_failed`。`run_fep` は窓ごとに全窓の reduced potential を `u_kn` として残し、`analyze_fep` は segment 鎖の連結 → 先頭 10 % 破棄 → `pymbar.timeseries` で subsample → MBAR、隣接 overlap < 0.03 を警告。

検証: (1) 真空 ACE-X-NME（amber14、Reference）で LEU→ALA / GLY→PRO / LEU→PHE ほかの端点差 < 1e-3 kJ/mol（`tests/test_fep.py::TestHybridVacuum`、slow マーク）。(2) 1L2Y（Trp-cage、20 残基）A:W6A、ff19SB/OPC、11,264 粒子（うち仮想サイト 2,737）: `build_hybrid_system` の端点差 0.12 / 0.12 kJ/mol（CUDA、`/tmp/fep_dev/study/jobs/folded/nodes/topo_003`）、`min` → `eq`（NPT）→ `run_fep` 21 窓 × (0.01 eq + 0.05 ns) が 192 s（718 ns/day、RTX A6000）、`analyze_fep` が dG = +7.6 ± 2.5 kJ/mol（folded leg のみ、独立サンプル 451、隣接 overlap 最小 0.079）。数値は流路確認用で、サンプリングは 2 桁短い。`fep → fep` 延長と `--lambda-indices` 部分実行（fep_001 → fep_002）も動作。(3) 調和振動子トイ問題で MBAR が解析解を再現。(4) `extract_tripeptide` が prep の merged.pdb から A:GLN5–TRP6–LEU7（60 原子）を切り出す。unfolded leg の実走はまだ。

付随変更: `node/constants.py`（`fep` 型、親規則、`CANONICAL_FORWARD_NODE_TYPE["fep"]="analyze"`）、`node/inputs.py`（`_resolve_fep_inputs` / `_resolve_analyze_fep_inputs`）、`node/lifecycle.py`（analyze 親の prod/fep/analyze 混在拒否 `analyze_parents_mixed`）、`_receipt.py`（fep の要約）、`_envelope.py`（fep を batch stage に、fep 後の `next` は `analyze_fep` を先頭に）、`guardrail_codes.py` に `fep_*` 20 コード、`tests/data/guardrail_codes.json` と `cli_contract.json` を再生成、`tests/test_envelope.py` のステージ順文字列に `fep` を追加。テスト: `tests/test_fep.py` 31 本、既存の registry / node / cli / envelope / contract / tempering 系 634 本 pass、ruff clean。罠: エラーコードは `code=` キーワード形式でないとスキャナが拾わない（前回の SST2 と同じ）。`run_fep` の `lambda_indices` 条件は宣言と同じ綴り（`"0-6"`）で照合するようにした（正規化した `0,1,…,6` と比較すると `--conditions` が必ず不一致になる）。

**追記（同日 12:30）— unfolded leg を流して ddG まで到達。** 同じ study に `jobs/unfolded` を追加（`record_study_plan --overwrite` で plan に job を足してから `bootstrap_md_workflow --job-id unfolded`；plan に無い job_id は bootstrap が拒否する）。`extract_tripeptide` の出力（GLN5–TRP6–LEU7）を `fetch_structure --source local` → `prepare_complex --cap-termini true --ph 7.4`（ACE4–…–NME8、72 原子、残基番号保持）→ `solvate_structure --dist 8 --water-model opc`（3,076 原子、29.4 Å 立方、Na⁺/Cl⁻ 2/2）→ `build_hybrid_system --mutation A:W6A --forcefield ff19SB --water-model opc`（64 s、端点差 0.040 / 0.038 kJ/mol）→ min → eq（NVT 0.02 + NPT 0.02 ns）→ `run_fep` 21 窓 × (0.02 + 0.2 ns) → `analyze_fep`: **dG_unfolded = −7.37 ± 1.47 kJ/mol**（独立サンプル 1,035、隣接 overlap 最小 0.099）。folded 側は fep_003 に子 `fep_005`（0.2 ns/窓、`--equilibration-time-ns 0`）を継ぎ、`analyze_fep` が 2 segment（50 + 200 サンプル/窓）を連結して **dG_folded = +12.18 ± 1.43 kJ/mol**（独立サンプル 1,364、overlap 最小 0.071）。`estimate_ddg --study-dir` → **ddG(W6A) = +19.6 ± 2.1 kJ/mol = +4.67 ± 0.49 kcal/mol（不安定化）**、`evidence/ddg_W6A.json` と `decisions.jsonl` に記録。符号・大きさは Trp-cage の W6 がコア残基で W6A が実質アンフォールド（実験の折り畳み ΔG は −1 kcal/mol 程度）という事実と整合するが、窓あたり 0.2–0.25 ns の流路確認であり、折り畳み側は変換中に構造が緩む可能性を制御していない（拘束なし）。位相分解: folded は decharge −15.0 / sterics +22.8 / recharge +4.4、unfolded は −19.4 / +9.0 / +3.0 kJ/mol で、差はほぼ立体 swap 由来。GPU は他ジョブと共有で 84–105 ns/day（専有時 718）。運用メモ: `singularity --no-home` で走らせると openmmforcefields が `~/.cache` に書けず `fep_endstate_build_failed`（`Read-only file system: '/home/yasu/.cache'`）になる。`--env XDG_CACHE_HOME=/tmp/... HOME=/tmp/...` を渡すこと（`container.md` の既知事項）。

未着手 / v1 の範囲外: 実験値との定量比較に足る長さのサンプリング（窓 ≥ 5 ns、複数 replica）、非平衡スイッチング、REST 併用、多重変異、電荷変化の有限サイズ補正、結合 ddG（complex / apo）の skill 化。SIF は pymbar 4.2 / HPacker 同梱の現行イメージのままで動く（依存追加なし）。

## 2026-09-18 — MODELLER のループモデルに芳香環の潰れ: 検査を入れて選択から外す（b6b7721、push 済み）、共有イメージは `modgeom-28f5e4a4f461`

T1R バンドルの 9 系を prep から作り直す作業（`/data1/rkp00079/rku00161/t1r/systems_v2/`）で、hum-ecd-9opw-apo の topo が `built_system_energy_implausible`（1.4e11 kJ/mol）で止まった。追うと、原因は prep の欠損残基補完が返したモデルにあり、PHE A53 と A373 の環が自分自身の上に畳まれていた（環の対角原子 0.34 Å）。MODELLER の出力 4 本のうち、初期ループモデルが PHE A29 / TRP A317 / TYR A333、ループモデル 2 が PHE A53 / A373 を潰しており、ループモデル 1 は VAL B364 の CG1/CG2 が 0.89 Å。DOPE はこれらを最下位に落とさないので、mdclaw は潰れたモデルを選んでいた。2026-09-10 に作った 9OPW の topo（6.2e10 kJ/mol、PHE A373 が同じ形）も同じ経路。

修正: `genesis/modeller.py` に `_model_geometry_issues()` を追加し、同一残基内の重原子 1.0 Å 未満、残基間 0.5 Å 未満（剛体部品を並べた鋳型は 1 Å 程度の接触を持ちうるので閾値を下げた）、Phe/Tyr/Trp の環対角 2.0 Å 未満を列挙。全モデルを検査し、合格の中で DOPE 最小を選ぶ（`selection_reason` に `_among_geometry_valid_models`）、落としたモデルは警告に列挙、全滅なら `modeller_models_geometry_invalid` で失敗。`structure/clean_protein.py` の prep 補完は、全滅時に種を変えて 8 モデルで 1 回だけ再試行する。実地で機能: 9OPW は 2 モデルとも不合格 → 再試行 8 モデル中 4 合格 → 健全なモデルで完了、merged に潰れ無し。テスト `tests/test_modeller_geometry.py`（平面環、畳まれた環、残基間 0.97 Å の接触は許容）、MODELLER/prep/genesis 系 251 本 pass。

イメージ: tempering-de7d17bb7ec5（main 7ee4be8）に 3 ファイルを重ねた `mdclaw-rikyu-arm64-cuda130-cufft121-modgeom-28f5e4a4f461.sif`（7,438,577,664 B、sha256 `fa683b6a…`、197 ファイルが `b6b7721` と一致）。受け入れは c287（job 123440、4 分 47 秒）で 3WD5 セル 58 s、水は剛体、500 反復正常、高速スイート 2,279 passed / 2 skipped。06:40 JST に共有パスを切り替え、ロールバックは `…pre-modgeom-20260918.sif`。RIKYU.md 両コピー更新。別セッションの SEUS 作業（`simulation/expanded_ensemble.py` ほか）は未コミットのまま、push にもイメージにも含めていない。

## 2026-09-18 — Pablo-path water was never rigid: fixed (8c0f83f, d11f8ed, a500ac8, pushed), shared RIKYU image switched to `water-011cf9894d92`

Found while auditing the T1R ECD bundle for Rikyu (`/data1/rkp00079/rku00161/t1r`, `run/rikyu_check_20260917.md`): hum-ecd-9opw-apo's shipped topo had OPC water with 2 constraints per water, an H–O–H angle term and hydrogens at 4 amu (O 10.016), while the other eight systems had rigid OPC. The one difference was the loader: 9opw was the only ligand-free system Pablo could parse, the others fell back to `PDBFile`. Mechanism, verified in OpenMM 8.5.1 `forcefield.py` (line 1300): `createSystem` recognises water by `res.name == 'HOH'` for both the rigid-water constraints and the `hydrogenMass` exemption; `PDBFile` rewrites WAT→HOH on read, Pablo keeps the file's names and the solvent fast path (b648068) copied them verbatim. Not water-model specific: sampled Pablo-built topos across the campaigns — v2 2axf (09-12, TIP3P), v4 1ewf (09-16, TIP3P, 581k atoms), sst2-trials 1kxv (09-16, OPC), seus-trials cln025 (09-17, OPC), and the 1AKE study of 08-01 (OPC) — all show 2 constraints per water, one angle term per water, O 10.015/10.016 and H 4.0; fallback-built 1dpt (09-16) is rigid. Of 1,505 completed topo nodes on disk, 702 built on or after 09-14 went through Pablo. The O–H constraints kept 4 fs stable, so nothing crashed; the water just ran with a flexible angle and heavy hydrogens. Any study whose topo node has `topology_validation.loader.used_pablo: true` should rebuild its topo node.

Fix in three commits on main, pushed: `_topology_pablo.py` names water `HOH` after every load path (fast path block and pure Pablo), taking the name set from PDBFile's own replacement table plus `WATER_NAMES`, guarded by composition (one O, ≤2 H, massless extra points allowed); a residue that is water by composition under a name nobody knows is reported as `unrecognised_water` and `build_amber_system` / `build_openmm_system` refuse with `water_residue_name_unrecognised` (user request: stop, never build such water silently). Tests: fast-path expectation now HOH, name-set and unknown-name tests added; loader, registry, CLI suites pass (registry files staged hunk-wise around the uncommitted SEUS entries). Verified the pure-Pablo branch too: cln025 rebuilt on a scratch DAG copy gives 3 constraints per water, O 16 / H 1.008, no angle terms.

Image: source overlay of the 4 changed packaged files on `protonation-7c66c59d41fe` → `mdclaw-rikyu-arm64-cuda130-cufft121-water-011cf9894d92.sif` (7,438,520,320 bytes, sha256 `e86b9179…`, all 196 packaged files equal `a500ac8`, evidence in `.validation/water-20260918/`). Acceptance on c179 (job 122757, 4.5 min): the 343k-atom WAT-named TIP3P 3WD5 cell through the Pablo fast path on the GPU gives 3 constraints per water, no water angle terms, masses 15.999/1.008, protein HMR intact; 500 iterations clean; fast suite 2267 passed / 2 skipped with the stale `test_pdb_writefile_inventory_is_pinned` deselected (unchanged since the SST2 commit). A first candidate (`dd3455f3faa7`, 8c0f83f only) also passed acceptance (job 122743, c149) and was discarded when the two follow-up commits landed. Shared path switched 00:32 JST with the usual atomic relink; rollback link `…pre-water-20260918.sif` → the protonation image. RIKYU.md (both copies) updated. Members need `git pull` because their clones overlay the image on the login node.

T1R side: 9opw's topo was rebuilt as `topo_005` with the patched loader (its solv artifacts restored from the shipped `system.prepared.pdb`; the deposit lacks PHE A373 and the MODELLER fill had collapsed its ring, which the `built_system_energy_implausible` guardrail caught — the side chain was rebuilt from an ideal template; the build adds a fixed 2.0 Å margin to the solv box, so the restored `box_dimensions.json` holds 144.72 Å for the 146.72 Å cell). Re-audit against topo_001 and 9opz: identical residues, disulfides (17), protonation, ions, box; water rigid; protein heavy atoms within 0.14 Å median.

## 2026-09-17 — Shared RIKYU image switched to `protonation-7c66c59d41fe` (main dd7a672); pi checkout updated; MDDataBench scorer-v05 merged

mdclaw: the protonation work was committed as `dd7a672` on main (only that session's hunks; the SST2 work in the same working tree stays uncommitted) and pushed; `pi update git:github.com/matsunagalab/mdclaw@main` moved the campaign skill checkout from `17283b6` to `dd7a672` (skills present). Image: source overlay of the six changed packaged files on the accepted SST2 candidate `sst2-26406c5ca5c3` (main `87f6862` + SST2 5590f4f) -> `mdclaw-rikyu-arm64-cuda130-cufft121-protonation-7c66c59d41fe.sif`, sha256 `fdcda5ab…`, all 196 packaged files equal `dd7a672` (`.validation/protonation-20260917/`, gitignored). GPU acceptance on c188 (job 122123): baked CLI shows the `no-prediction` description, CUDA present, 3WD5 cell in 75 s, 500 CUDA iterations clean, fast suite 2265 passed / 2 skipped / 1 failed — `tests/test_pdb_export_resname_guard.py::test_pdb_writefile_inventory_is_pinned`, whose pin does not list `simulation/tempering.py`'s `PDBFile.writeFile` added by the SST2 commit `5e2d902`; it fails the same way at `87f6862` with the previous image, so it is a stale test pin for the SST2 author to settle (restore helper or reclassify), not a runtime defect. Switched at 13:37 JST with the usual atomic relink; backup link `…pre-protonation-20260917.sif` -> the v2fix image of campaigns v3/v4/glm. No process was using the shared path at the switch. MDDataBench: branch `scorer-v05` merged into main as `22d2fe3` (dataset v0.5, tolerant scorer, declared-site metal exemption); the other session's uncommitted memo entries on main were kept in place. Next campaign runs on dataset v0.5 with this image and checkout; its spec must pin `sif_sha256 fdcda5ab…`.

## 2026-09-18 — `analyze_tempering`: SST2 walker の MBAR 再重み付けと重み収束判定を analyze ノードに

SST2 の解析が DAG の外の手書きスクリプト（`analysis_interim/`）にしかなく、エージェントには再現できない状態だったので、`mdclaw/analyze/tempering.py` に `analyze_tempering`（`@node_tool(node_type="analyze")`）を追加した。親は `run_sst2` の prod 葉（walker 1 本 = 親 1 つ、`production_chain` なら `continue_from` 鎖を古い順に束ねる、`segment` なら葉だけ、`comparison` は `tempering_scope_unsupported` で拒否）。`tempering.csv` の各行（交換試行ごと、2 ps）から u_m(x) = β_ref [Σ_f λ_m^f E_f + √λ_m E_pw] を全 rung について組み（E_ww と solute 非スケール項は rung 間で相殺）、pymbar 4.2 の MBAR を全 walker プールで解く。出力は `weights.json`（f_k を kJ/mol のリストで、次段の `run_sst2 --weights-file` にそのまま渡す）、`tempering_frames.csv`（DCD フレームごとの walker、node、frame、chain_frame、step、time_ns、rung、温度、log_weight、weight。surrogate 用の λ 付きデータ形式）、`tempering_mbar.json`、`tempering.png`（walker ごとの rung 時系列と、on-the-fly 重み・walker 単独 MBAR のプール f_k からの偏差）。`verdict` は全 rung 訪問、各 walker の on-the-fly 重みと単独 MBAR がプール f_k から 2.5 kJ/mol（300 K の 1 kT）以内、往復 5 回以上、rung 変化率 0.1 以上のすべてを満たせば `weights_converged`、でなければ `weights_drifting` と `verdict_reasons`。`discard_ns`、`fixed_weights_only`、`row_stride` あり。フレーム行は `output_frequency_ps` / `timestep_fs` から step の倍数で同定し、DCD のフレーム数は mdtraj を使わずヘッダを直接読んで突き合わせる（mdtraj の dcdplugin は stdout に書くので CLI の JSON を壊す。実際に壊れたので直した）。エラーコードは `code=` キーワード形式でないと登録スキャナが拾わない（前回と同じ罠）。

検証: 閉じた形で f_k が分かる 1 次元ガウス模型（E_pw = a·x、rung k の分布は N(−c_k, 1)、f_k = −c_k²/2）で MBAR が 0.1 kT 以内に f_k を再現、frames 表の整列、`segment` / `fixed_weights_only` / 混在ラダー拒否 / 非 SST2 親拒否を `tests/test_analyze_tempering.py`（9 本）で固定。1KXV の実データを DAG 経由で流した結果: H3 のみ（analyze_001、prod_008 + prod_011）f_k = 0 / 255.7 / 466.2 / 643.4 / 787.9 kJ/mol（手計算の MBAR と一致）、フレーム ESS 2,465 / 10,000、判定は `weights_drifting`（on-the-fly 重みとの差 9.4 kJ/mol、walker 単独 MBAR の最上段が 779.6 vs 795.1 で 15.5 kJ/mol 差）。つまり前回「数 kJ/mol で一致」と書いたのは甘く、最上段は seed 間で 6 kT 違う。50 ns では未収束で、延長が要る。H3 + 殻（analyze_002）も同様。二面角のみ（analyze_003）は seed 1 が往復 1 回・重み 25 kJ/mol ずれで罠、seed 2 は 418 往復。`run_sst2` 側は SST2 が残す `sst2_sst2.pdb/.cif` を消すようにした。skill: `skills/md-analyze/tempering.md` 新設、`md-analyze/SKILL.md` と `metrics.md` から誘導、`md-production/sst2.md` を 2 段階（適応 → `analyze_tempering` → 固定重み）の手順に書き直し、`MDCLAW_SST2_HOME` 必須という古い記述を消した。`sst2_not_installed` の案内文も同梱前提に変更。

共有イメージへの反映（同日 01:34 JST）: `.validation/tempering-20260918/build-tempering-candidate.sh` で water イメージ `011cf9894d92`（main a500ac8 + SST2 5590f4f）に 4 ファイル（`analyze/tempering.py` 新規、`analyze/__init__.py`、`guardrail_codes.py`、`simulation/tempering.py`）をソースオーバーレイ → `mdclaw-rikyu-arm64-cuda130-cufft121-tempering-de7d17bb7ec5.sif`（7,438,561,280 バイト、sha256 `70d911f5…`、197 ファイルが 7ee4be8 と一致）。GPU 受け入れ（job 122840、c179、4 分 19 秒、`/data1/rkp00079/rku00161/mdclaw-validation/tempering-20260918/`）: 焼き込みパッケージの CLI に `analyze_tempering` が載る、CUDA あり、1KXV job のコピー（DCD 抜き）上で DAG 経由の `analyze_tempering` がログインノードと同じ f_k（最上段 787.9）と 10,000 フレームを再現、CUDA で `run_sst2` 20 ps が prod ノードとして完了し残骸なし、高速スイート 2276 pass / 2 skip（`test_pdb_writefile_inventory_is_pinned` は従来どおり除外）。いつもの atomic relink で共有パスを切り替え、巻き戻しリンク `…pre-tempering-20260918.sif` → water イメージ。RIKYU.md（両コピー）更新。skill の変更は各メンバーの `git pull`（main 7ee4be8）で届く。

## 2026-09-17 — VHH surrogate のデータセット生成方針: アンサンブルのみ、SST2 全 rung を λ 付きで教師に、3 層の配列セットで 1 万–1.5 万 GPU 時間

1KXV の 50 ns ブロック完走後の数字で見積もった。SST2 walker 6 本を MPS で 1 GPU に詰めると 300 ns が 4 時間 21 分（集計 1,650 ns/GPU 日、0.0145 GPU 時間/ns、1 本 275 ns/日）。ユーザー案「1,000 VHH × 3 レプリカ × 1 µs」は 3.0 × 10⁶ ns → 約 43,600 GPU 時間（1,820 GPU 日、330 円/GPU 時で約 1,440 万円）。緩衝 10 Å + 8 本詰めなら推定 30,000–36,000（未実測）。詰めた状態では SST2 の追加コストは通常 MD の詰め込みとほぼ同じで、単独走行の 2 倍の減速は交換ステップの待ちが MPS で隠れるため出ない。

ユーザー決定: surrogate は動力学ではなくアンサンブル（300 K の p(x | 配列)）でよい。これを受けた生成方針（`docs/research/sst2-seus-plan.md` §7「データセット生成の方針」に記載）: (1) SST2 の全 rung のフレームに λ と MBAR 重みを付けて保存し、温度条件付き生成モデルの教師にする（300 K 再重み付けだけだと ESS は 25 %）。(2) 予測構造（Boltz-2）から出発し、solute は Kabat 位置で定義。(3) 配列セットは幅（1,000–2,000 × 1 × 200–300 ns）、深さ（100 × 3 × 1 µs）、変異近傍（20 親 × 30 変異体 × 300 ns）の 3 層で合計 1 万–1.5 万 GPU 時間。(4) 幅の層の途中でモデルを学習し、再重み付け ESS の低い配列を次に回す能動的ループ。動力学用の 300 K 群れ MD は不要になった。

---

## 2026-09-17 — `protonation_method` "standard" renamed to "no-prediction"; the CLI listing and the prep receipt now say what the method did

Why: in the kimi-k3 v4 and glm-5.3-flash campaigns four cli_skill_sif attempts (008 r2, 013 r1, 069 r2; glm 025 r2) translated the prompt's "standard state at pH 7" into `--ph 7.0` and left propka running, and propka moved an ionisable residue off the fixed state in about 1 of 10 such attempts (36 of 39 `--ph`-only attempts passed; 214 of 214 with the fixed-state method). The name "standard" read as "the standard method", `--list-json` carried no parameter descriptions at all, and the receipt never mentioned HIP/ASH/GLH, so nothing corrected the agent before MD.

Changes (uncommitted, mdclaw only; the working tree also holds another session's pending restraints/receipt edits, left as they were):
- `clean_protein.py`: `PROTONATION_METHODS = ("propka", "no-prediction")`, `DEPRECATED_PROTONATION_METHODS = {"standard": "no-prediction"}`, `normalize_protonation_method()` (alias accepted with the warning "protonation_method 'standard' is deprecated; use 'no-prediction'"), node label `pdb2pqr_no_prediction`; `prepare_complex.py` normalises the alias after the result envelope is created and now copies `protonation_method`, `protonation_states`, `histidine_states`, `requested_ph` from each `clean_protein` result into the per-chain record; `protonation.py` hint updated. Default stays `propka`.
- `_cli.py`: `_docstring_parameter_descriptions()` parses the Google-style `Args:` block, so `--list-json` parameters carry `description` (prepare_complex: 39 of 41 parameters); the two that mattered now read "propka (default) predicts ... no-prediction skips the prediction and keeps the force field's fixed states ... A request for 'standard states' means no-prediction, not --ph 7.0" and "pH for propka (default: 7.4); ignored by no-prediction". The contract golden pins names/required only, so it is unchanged.
- `_receipt.py`: prep facts gain `protonation` (method, label, pH for propka, `non_fixed_states` = ASH/GLH/LYN/HIP plus CYM outside declared metal sites) and the summary line ends with e.g. `protonation propka pH 7.4 (2 non-fixed: GLHA303, HIPA56)` or `protonation no-prediction`.
- Skills: `md-prepare/prep-chemistry.md` section "Fixed states versus predicted ones" (the 1-in-10 figure, `--ph` is not a substitute, read the receipt), one sentence in `md-prepare/SKILL.md` step 4; `md-prepare/membrane.md` gains "What decides the embed time" (cache keys, bundled compositions, keep the defaults), the legacy backend bullet marked debugging-only, and a cold-build paragraph (preparation, not Slurm MD; never start one on a shared CPU host) — the two membrane failures of the glm campaign. `tool-reference.md` names the two methods.
- Tests: pins moved to the new name in `test_protonation_method.py`, `test_protonation_states.py`, `test_envelope.py`; new `tests/test_protonation_method_alias.py` (alias + warning, refusal list, list-json descriptions, receipt fact with a metal-site CYM excluded, no-prediction summary). `ruff check mdclaw/ tests/` clean; focused suite 384 passed, 1 skipped, run through the SIF overlay (no conda on this host).

Campaign note: the pi package checkout that campaigns read (`~/.pi/agent/git/github.com/matsunagalab/mdclaw`, pinned at 17283b6) does not see these skill edits until `pi update git:github.com/matsunagalab/mdclaw@main` after the next merge; the image needs a rebuild for the CLI/receipt changes. Do neither while a campaign runs.

## 2026-09-16 — Scorer recalibration: what the two campaigns say the prep and md axes should tolerate

Background: MDDB project metadata (80 fields per project, checked live on A01M3, A0001, A002J) has no pH or protonation field; the only protonation record is the reference topology's residue names, from which the task builder derives `stated_protonation` (6 tasks name protonated histidines: 007, 009, 014, 088, 096, 100). The other 92 references are all-standard because that is how they were built (MoDEL entries with Amber 8 / Parm99, the newer ones with pdb2gmx defaults), not because anyone chose pH 7. So the v0.4 sentence "every ionisable side chain in its standard state at pH 7" encodes a build convention, and the four propka failures (kimi v4 008 r2, 013 r1, 069 r2; glm 025 r2) measure convention-matching. Decision with the user: dataset v0.5 drops the blanket sentence ("neutral pH; ionisation states of the side chains are your choice"), keeps the six named-residue sentences (with the residue name, e.g. HIP, so it maps onto `--protonation-states`), and the scorer tolerates ±1 H on ASP/GLU/LYS/HIS/CYS the task does not name; named residues stay strict. mdclaw keeps `propka` as the default; `standard` is to be renamed (`no-prediction`), described in `--list-json`, and reported in the receipt with the residues propka moved.

Prep axis, second change: the metal-ligand exemption is read from `minimized_structure.pdb` at 3.5 A, so 062 r3's zinc leaving two thiolates during minimisation turned CYM224 into a composition failure while r1/r2 with identical flags passed. Derive the exemption from prep's declared `metal_sites` instead.

md axis: no loosening warranted. Over the 588 skill attempts of kimi-k3 v4 and glm-5.3-flash, 586 radius-of-gyration checks and 562 fluctuation-magnitude checks were parsed against their calibrated bands (reference 1 ns windows, n=30, widened by 4 window SD; the lower fluctuation bound is diagnostic). Only 2 checks failed, both just outside the upper edge: 048 r2 Rg over by 1.5 % of the band width, 040 r3 fluctuation over by 1.0 %; both were the shortest-equilibration replicate of their task. Inside the band the median distance to the nearest edge is 43 % of the width, the 5th percentile 16-27 %, and 2-3 checks per axis sit within 5 % of an edge. A false-fail rate of about 2 in 1,150 is consistent with the calibration's intent (an unwidened band was measured to reject a correct submission 7-16 % of the time), so the bands stay; the two failures are the price of cutting equilibration, which the skill's default protocol avoids. Twelve fluctuation values fell below the lower bound (nucleic tasks 047, 053, 056, 057, 059), all reported as diagnostic and passed, which is the intended behaviour for stiff DNA/RNA runs.

## 2026-09-16 — kimi-k3 v4 cli_skill_sif: 289 of 294; the five failures by cause, and the propka question

v4 finished 21:14 JST (882/882; cli_sif 282/294, cli_skill_sif 289/294, sif_only 32/294). All five skill-condition failures are 19/20 evaluation failures; none is a timeout, a CLI error or a gateway loss. By cause:

**A. propka assigned a non-standard ionisation state although the prompt asks for standard states (3).** 008_membrane_6i53 r2 (GLH303, HIP56), 013_membrane_6ps2 r1 (ASH79), 069_soluble_1aol r2 (HIP112). Each ran `prepare_complex` with `--ph 7.0` (or `--ph 7`) and without `--protonation-method standard`; the passing replicates of the same tasks used `standard` (013 r2 even re-ran with it after a first `--ph 7.0` prep; 069 r3 patched HIP112 with `--protonation-states '{"A:112":"HID"}'`). The reasoning is the same in all three and in glm's 025 r2: "propka at pH 7 would give the standard states anyway; to be safe pass --ph 7.0". Two of the three had read `prep-chemistry.md`, whose lines 100-108 say to pass `--protonation-method standard`; the rule was read and rationalised away. Across both campaigns' skill attempts: with `--protonation-method standard` 214 of 214 passed; with `--ph` only, 36 of 39 passed, and all four campaign failures of this kind (kimi 3, glm 1) are in that group, so propka moves an ionisable residue off its standard state in about 1 attempt in 10. The scorer has no tolerance for it by design (`residue_atom_counts_match_reference` is tautomer-blind but counts every ionisation variant; the only exemptions are metal ligands and catalytic dyads), and every v0.4 prompt carries the sentence "Simulate every ionisable side chain in its standard state at pH 7". Whether that sentence and the strict count stay is a dataset decision (see below).

**B. Equilibration cut short, MD statistic lands just outside the reference window (1).** 048_nucleic_1h9t r2: Rg 20.564 A against [19.436, 20.547] A, eq NVT 0.1 + NPT 0.5 ns and prod 1.0 ns; r1 and r3 ran the skill's 1 + 1 ns and 1.1-1.2 ns prod and measured 20.22 and 19.91 A. Same shape as glm's 040 r3 (0.740 vs 0.736 A with the shortest protocol of its three).

**C. Zinc site came apart at minimisation and the scorer's distance-based exemption then exposed a thiolate (1).** 062_metal_6w9c r3: prep, topo and min were flag-identical to r1 (chains C + I, ZN C402 `ZN-Cys3` site established, CYM189/192/224, ff14SB non-bonded zinc, `run_minimization --restraint-atoms solute_heavy --restraint-force-constant 100`), but in the minimised structure r1 keeps all three SG at 1.9 A from the zinc while r3 keeps only CYM192; the ion (not covered by the solute-heavy restraint) left two thiolates. The scorer reads metal ligands from `minimized_structure.pdb` with a 3.5 A cutoff, so r3's exemption set fell from 6 to 5 residues and CYM224 (10 atoms) was compared against the reference's CYS224 (11). Two fix candidates: mdclaw should restrain or bond an established metal site through min/eq (and the topo receipt should state the metal model used; today `applied.facts` has no metal entry); MDDataBench's exemption should come from the declared site (prep `metal_sites`) rather than a frame.

Pattern across kimi-k3 v4 and glm-5.3-flash (10 failures in 588 skill attempts): 4 propka-vs-standard, 2 shortened equilibration, 2 membrane build choices (glm only), 1 zinc-site collapse, 1 infrastructure segfault. No failure came from the CLI being hard to operate.

**The propka question (user, 2026-09-16: "unless specified, propka variance was meant to be tolerated").** As shipped it is not tolerated, and the prompt does specify: the standard-state sentence is in all 98 tasks, and the MDDB references were built with standard states. If the benchmark should instead accept any ionisation the agent's pKa predictor chooses for residues the prompt does not name, two things change together: the sentence is rewritten to say so, and `compare_monomer` gets an ionisation tolerance (±1 H on ASP/GLU/HIS/LYS/CYS not named in the task) alongside the existing tautomer tolerance; the four failures above and qwen's 023 would then pass (kimi-k3 v4 skill 292/294, glm 291/294). Recommendation: keep the sentence and the strict count. The reference is at standard states, a HIP or ASH changes the charge and the dynamics the md axis compares, and following an explicit protonation instruction is part of what the prep axis measures; the 1-in-10 propka deviation is real chemistry the agent is asked to override, and the skill already says how. Decision pending with the user.

## 2026-09-16 — Root causes of every glm-5.3-flash cli_skill_sif failure (5 of 294), from the transcripts

All five read the skills (36-39 files each); none is a CLI-operation error. Each failure is one decision, and for four of them the transcript shows the reasoning.

1. **002_membrane_5zk3 r1, timeout at the solv node.** Thinking: "Default example uses dist 15, dist-wat 17.5 — that creates a big box (long build time and big system). For speed, smaller box is better: dist 10.0, dist-wat 10.0." The agent did not know `dist_wat` is part of the patch-cache fingerprint; the miss started a CPU patch equilibration on the login node. `membrane.md` mentions `dist_wat` only in the Packmol-race retry paragraph ("keep leaflet and dist_wat unchanged unless the user explicitly asks") and says "common compositions are pre-built" without naming them or the parameters that select them. The agent read the page 11 times and still optimised the wrong variable. Fix: state next to the embed example that lipids, ratio, water model, salt, `dist_wat`, `leaflet` and `patch_side` select the cached patch and that changing them rebuilds it on the host; list the bundled compositions; CLI refusal on a CPU-host miss (known-issues).

2. **010_membrane_6kux r3, timeout at the solv node, detached script.** Thinking: "The patch-tile cold build runs MD (OpenMM equilibration of the patch) ... strictly, the patch equilibration inside embed_in_membrane is MD computation [that must go through Slurm] ... is DPPC pre-built in the bundled cache? If not, the cold build happens ... 3. Use packmol-memgen legacy backend: no patch cold-build." The agent chose the legacy full-box backend to avoid a cold build that would not have happened (DPPC/TIP3P at the default geometry is bundled; r1 and r2 embedded in about 70 s), and the legacy path ran packmol for 25+ minutes on the CPU. Two gaps: there is no way to ask the CLI whether a composition is cached (`probe_patch_cache` exists in `patch_membrane.py` but is not a tool), and `membrane.md` lists `--membrane-backend packmol-memgen` as a plain option without saying it is slower and CPU-bound. Fix: expose the cache probe (or report it in `explain_node` for a solv node), and mark the legacy backend as a debugging path in the skill; CLI warn/refuse on CPU hosts (known-issues).

3. **025_complex_1akj r2, 19/20, HIP3 and HIP151.** Thinking: "prepare_complex presumably runs protonation via PDBFixer/PDB2PQR at pH 7. Default. His neutral ... At pH 7.4, propka would still usually assign neutral HIS (pKa ~6.5). But to be safe, pass `--ph 7.0`." The rule "when the request asks for standard states pass `--protonation-method standard`" lives in `prep-chemistry.md` (lines 100-108); r1 read that page and passed, r2 read 36 skill files including `defaults-and-guardrails.md` but not `prep-chemistry.md`, and the spine (`SKILL.md` step 4) only says to read it "for the protonation baseline". The receipt then reported nothing about histidine states, so the propka result went unnoticed. Fix: put the one-line standard-state rule in the spine (or in `defaults-and-guardrails.md`, which every agent reads), and add the histidine/ionisable-state tally and the method used to the prep receipt facts.

4. **040_ligand_3n2u r3, 19/20, fluctuation 0.7400 A against the reference window [0.3398, 0.7360].** The three replicates ran the same system (1,269 solute heavy atoms, 4 fs HMR, 298 K) with different protocols: r1 NVT 1 ns + NPT 1 ns, prod 1.25 ns, fluctuation 0.62 A; r2 0.1 + 0.15 ns, prod 1.2 ns, 0.67 A; r3 0.1 + 0.1 ns, prod 1.0 ns, 0.74 A. The value rises as equilibration and production shrink, so this is the shortest protocol landing 0.004 A above the window's upper edge; r1 shows the 20-minute MD limit allowed the skill's default 1 + 1 ns. Not a defect anywhere; a budget shortcut that cost the margin.

5. **090_soluble_1aa3 r3, `production_incomplete`.** eq job 114913 died 14 s into `singularity exec --nv` on c165 with a bare "Segmentation fault" before any OpenMM output; min on the same node had completed 10 s earlier; the only segfault in this week's Slurm logs across all campaigns. Reset with `--force true` and rerun: 20/20 in 302 s.

Pattern: 3 of 5 are the model trading the skill's defaults for speed or caution (smaller box, legacy backend, shorter equilibration), 1 is a routing miss inside the skill (the rule sat on a page the agent did not open), 1 is infrastructure. Skill-side fixes recorded in `roadmap-and-known-issues.md` under "Skill Text Fixes from the glm-5.3-flash Campaign".

## 2026-09-16 — 1KXV SST2 試走: min ジョブが `singularity: command not found` で落ちた原因と CLI 側の対策

study `/data1/rkp00079/rku00161/sst2-trials/1kxv-h3`（bootstrap → prep_001 鎖 C 119 残基 → solv_001 OPC 80.2 Å → topo_001 ff19SB/OPC/HMR 71,013 原子）まではログインノードで完了。min_001 を Slurm に投げたところ 1 秒で FAILED、afterok 鎖の eq と prod 7 本が全部キャンセルされた。ジョブの stderr は `slurm_script: line 14: singularity: command not found`。原因: 提出を SIF 内の `mdclaw`（MDDataBench の試作ラッパー `runs/prep/bin/mdclaw`）で行ったが、そのラッパーは `MDCLAW_SLURM_PATH` を渡さない。CLI は sbatch に渡す環境の PATH を `MDCLAW_SLURM_PATH` + システム標準から組むので、未設定だとイメージ側の PATH（`/opt/mdclaw/bin` …）になり、生成スクリプトが裸の `singularity` を呼んで計算ノードで見つからない。`skills/hpc-run/sif-slurm.md` は `--env MDCLAW_SLURM_PATH="$PATH"` を指示しており、ページどおりに呼ばなかった自分の手順ミスだが、CLI が「実行できないジョブ」を黙って提出したのも問題。`export MDCLAW_SLURM_PATH="$PATH"` で min_002（117621、13 s で完了）→ eq_002（117642）→ MPS 1 ジョブに SST2 ウォーカー 6 本（117647、`submit_mps_job`、50 ns ブロック、10 h 上限）と md_ref（117648、100 ns）を afterok で投入し直した。

CLI 側の対策（同日、commit 予定）: `mdclaw/slurm/config.py` に `resolve_container_runtime()` を追加し、`submit_job` / `submit_array_job` / `submit_mps_job` が提出前にランタイム（`singularity` → `apptainer`、または `configure_container --runtime` の指定）を `MDCLAW_SLURM_PATH`（なければ PATH）上で絶対パスに解決して sbatch スクリプトに書く。ユーザーの「変数を書かせずに済ませたい」に応えて、イメージ内で `MDCLAW_SLURM_PATH` が無いときはコンテナを起動した親プロセス（launcher）の環境を `/proc/<ppid>/environ` から読み、ホストの PATH を自動導出する（`_base.host_search_path`、PID 名前空間は共有なので読める）。それでも見つからなければ `container_runtime_not_found` で拒否、イメージ外なら警告のみ。スキルには「通常は何も設定不要、拒否されたら hint に従う」とだけ書いた。ユーザーの指摘で `run_sst2` に溶媒混入ガード（`sst2_solute_includes_solvent`: 水・イオン・仮想サイトが solute に入っていれば拒否、mdtraj DSL の `resid` は全体番号なので起きやすい）も追加。今回の index ファイルは protein 鎖 0 のみで混入なし。

## 2026-09-16 — SST2 同梱イメージ `sst2-0a15ed1cf7d1`: run_sst2 が SIF だけで 1KXV の DAG 上を走る

`.validation/sst2-20260916/build-sst2-candidate.sh` で、現行の共有 SIF（v2fix-f0491786d1f8）から overlay ビルド: HEAD（bc2bb8c）と差のある mdclaw の 12 ファイルを差し替え、fork `matsunagalab/SST2@5590f4f`（版 `0.0.1+mdclaw.1`）を `%post` で pip install（イメージ内の conda git は https remote helper を持たないので、ホストで clone した pin 済みの木を `%files` で持ち込んで local path から入れる。`pdb_numpy 0.0.12` は PyPI から）。`MDCLAW_SST2_REVISION=5590f4f` を環境と label に記録。検証: パッケージ 196 ファイルが HEAD と一致、`import SST2` 可、`mdclaw --list` に `run_sst2`（26 引数）。`tests/test_tempering.py` は SIF 内蔵パッケージだけで 7 本 pass。

受け入れ: キャンペーンの 1KXV job（source→prod_001 完了）をトラジェクトリ抜きで複製し、`create_node --node-type prod --parent-node-ids eq_001` → `mdclaw --job-dir … --node-id prod_002 run_sst2 --solute-selection "chainid 0 and resid 96 to 108" --temperatures-kelvin 300 357 424 505 600 --simulation-time-ns 0.02 --pressure-bar 0 --platform CUDA`。`MDCLAW_SST2_HOME` なし・PYTHONPATH 空で GPU 上 690 ns/day、prod_002 completed、solute 198 原子、境界 exception 40、bucket 14/12/14/567、20 ps で rung 5 本を訪問。途中で 2 つ直した: CLI は `list[float]` を nargs に展開しないので `temperatures_kelvin` を `list[str]` に（bc2bb8c）、`run_sst2` は node context 必須なので受け入れも DAG 経由に。イメージは `/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-sst2-0a15ed1cf7d1.sif`（ユーザーが配置、sha256 0a873e28…eb82 を確認）。**共有名 `fusefix-6f171d2f0fa5.sif` の symlink は v2fix のまま**（kimi-k3 v4 キャンペーン走行中、pin を動かさない）。既知の nit: 成果物に SST2 由来の `sst2_sst2.pdb/.cif` が残る（無害、次のイメージで掃除）。

## 2026-09-16 — NVIDIA MPS で小系レプリカを 1 GPU に詰める: `submit_mps_job` 実装と GB200 実測（4 本で 2.24 倍、8 本で 2.65 倍）

NVIDIA の記事 "Maximizing OpenMM Molecular Dynamics Throughput with NVIDIA Multi-Process Service" (2025) に沿って、複数の DAG ノードを 1 GPU ジョブに同居させる `mdclaw submit_mps_job` を追加した（`mdclaw/slurm/mps.py`、`sbatch.py` の `_generate_mps_sbatch_script`）。RIKYU の Slurm は `GresTypes=gpu` のみで `gres/mps` は無いので、ジョブ自身が `--gpus=1` の割当内で MPS 制御デーモンを起動する（ジョブごとの pipe dir を `$TMPDIR` 下に置く。Apptainer はホスト `/tmp` を見せるのでコンテナ内クライアントもそこへ繋がる。ログは `<job>_<id>.mps/`）。`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE = 200/N`（記事の推奨、1–100 にクランプ）。各タスクはバックグラウンドで同時起動し、`CUDA_VISIBLE_DEVICES` の GPU に round-robin、全 `wait` 後にデーモン停止、1 つでも失敗すればジョブ失敗。N 個のノードは同じ `slurm_job_id` と `slurm_mps_slot`、スロット別ログを持ち、`check_job` は 1 job id を全ノードに反映する（`_find_records_by_job_id`；完了済みノードは降格しない、失敗ノードは自スロットの stderr を証拠にする）。`--platform CUDA` 明示を要求（`mps_task_requires_cuda_platform`）、GPU あたり 16 本超は拒否（`mps_tasks_per_gpu_exceeded`）。テスト 11 本追加、関連スイート 402 本通過。スキル: `skills/hpc-run/submit-mps.md` 新設、hpc-run / md-production / md-study(compute-budget) / run-loop から「小系（〜10 万原子）のレプリカは既定で MPS 詰め」と誘導。ユーザー指示: 小さい系のレプリカは GPU 時間節約のため可能な限り MPS で流す。

実測（`/data1/rkp00079/rku00161/runs/mps-bench-20260916`、hen lysozyme 1AKI、ff19SB/OPC、10 Å、51,832 原子、HMR 4 fs、NPT 300 K、各レプリカ 3 ns、GB200 1 基、コンテナ起動込みの tool_started→tool_completed で計時）:

| 条件 | 同居数 | ジョブ経過 | レプリカ 1 本の壁時間 | 集計 ns/day/GPU | 倍率 |
|---|---|---|---|---|---|
| 単独 `submit_job` (117404) | 1 | 4:42 | 277 s | 919 | 1.00 |
| MPS 4 本 (117405, ATP=50) | 4 | 8:24 | 495–498 s | 2,057 | 2.24 |
| MPS 8 本 (117407, ATP=25) | 8 | 14:11 | 837–845 s | 2,437 | 2.65 |
| 対照: MPS なし 4 本同時 (117408) | 4 | 22:28 | 1,341 s | 769 | 0.84 |

結論: (1) この規模では 8 本詰めが最良で、単独比 2.65 倍、4 本詰め比でも +18 %。1 本あたりは 3 倍遅くなるので `--time-limit` は N×単独時間/倍率で見積もる。(2) MPS 無しの同時実行（タイムスライス）は単独逐次より遅い（0.84 倍）。「同じ GPU に複数プロセスを流す」だけでは逆効果で、デーモンが必須。(3) 4 ジョブ計 0.83 GPU-h（≈250 円）。12 ns 分のサンプリングが 18.8 GPU 分 → 8.4 GPU 分（4 本）、24 ns 分が 37.6 → 14.2 GPU 分（8 本）。`skills/hpc-run/submit-mps.md` の既定は「5 万原子級まで 8 本、10 万原子まで 4 本、10–40 万原子は 2 本」に更新。md-study の予算導出は 4 本詰めで 1.5 倍を計画値のまま（実測 2.24 倍、10 万原子近くでは下がる見込み）。

MLOPart（NVIDIA ブログ 2025-12、MPS v3 では locality domains）は Blackwell の 2 ダイをそれぞれ CUDA デバイスに分けてメモリ局所性を上げる機能だが、driver 590(x86)/595(ARM, CDMM)+CUDA 13.1 が必要で対応表は B200/B300 のみ。RIKYU は 580.173 で、ログインノードで `start_server -mlopart` を試すと無警告で通常サーバが立つ（`device_query` に MD サブデバイス無し）。ユーザーと相談し実装せず、`docs/developer/roadmap-and-known-issues.md` に追記のみ。

スキルはサイト特化にしない（ユーザー指示: 他の GPU でも LLM が外挿して MPS 投入を判断できること、記述は RIKYU ではなく GB200 と書く）。`submit-mps.md` は GPU クラス × 原子数の判断表（NVIDIA の H100/L40S/A10 データと本日の GB200 実測を参照点に、原子数 2 倍で同居数半減・小さい GPU では 1 段下げる外挿則）、MPS 可否のチェック（Volta 以降、`nvidia-cuda-mps-control` の存在、compute mode、丸ごと GPU 割当、`--platform CUDA`）、GPU 不明時の較正手順（単独 1 本 vs 4 本詰めの短い 2 ジョブで集計 ns/day を比べ 1.3 倍以上なら詰める）で構成した。

## 2026-09-16 — SST2 fork: 部分 solute の 2 つの欠陥を修正、テストで固定

`matsunagalab/SST2` を `~/SST2` に clone（upstream `31c76a4`）、ブランチ `mdclaw`。SIF の Python（OpenMM 8.5.1）に `pdb_numpy` を `~/SST2/.pylib` へ `pip --target` で足し、`PYTHONPATH=src:.pylib` で上流テストが CPU で走る（5 本 72 s）。上流 HEAD は 5 本中 2 本が失敗する: commit 89a4de6 で `compute_all_energies` の `E_solvent` が溶媒 NB を含まなくなったのに `test_rest2.py:447` と `test_sst2.py:178` が追随していない。受理判定には使われない量なので物理には影響しない。

部分 solute（2HPL テスト系の鎖 B 残基 2–4、鎖 A 残基 11–20）で `test_rest2_partial_solute.py` を書き、HEAD で失敗を確認してから修正した。(1) 境界をまたぐ `NonbondedForce` の 1-4 exception（30 個）が未スケール。`find_solute_nb_index` で別に集め `update_nonbonded` で chargeProd と ε を √λ 倍（gREST の k=1, l=2）。(2) 副 System を PDB テキスト経由で作り直すため、solute を除いた鎖 B で残基 1 と 5 が隣接扱いになり、偽のペプチド結合 ASP1:C–GLY5:N（0.82 nm、9.6 万 kJ/mol）と exception 15 個が溶媒副 System に入っていた。Modeller の Topology から直接 `createSystem` するよう変更。数値: 「非スケール」E_pw の λ=1→0.5 のずれが 112 kJ/mol（10 %）→ (1) で 7 kJ/mol → (2) で相対 1e-4 以内。溶媒副 System と全系の ww 部分の差は 17.2 → 0.0001 kJ/mol。鎖全体 solute の上流ケースは修正前後で不変（3 pass 2 fail のまま）。コミット 82d5208（失敗するテスト）と c1ee442（修正）。

同日、続けて fork 側の Phase 1 実装を終えた: cef5d43 複製方式の副 System ビルダー（`SST2/subsystem.py`、`REST2(subsystem="copy")`、ForceField 不要、PME 設定を丸写し。solute 副 System の NB は全系で溶媒を零にした値と 0.05 kJ/mol 以内、forcefield 方式と項ごとに一致）。1f87513 受理の Gibbs 化と `seed`、固定重みの修正（従来は `weights=` を渡すと走行平均が未初期化で reporter が落ち、重み自体も受理に使われていなかった）、有効重みのレポート出力。スタブで 2 万回引いて厳密な条件付き分布と 4σ 以内、neighbor 方式は詳細釣り合いを満たす。369c240 XML 三点セットのドライバ `SST2/driver.py` と sidecar による継続。テスト 19 本 pass（上流由来の 2 本は既知の失敗）。テスト fixture で `VerletIntegrator(1.0)` の Context に `setVelocitiesToTemperature` すると速度拘束が step size を使うため 444 nm/ps の速度が出て NaN になった。step size 0.001 で解消。実データの state.xml には無関係。計画 `docs/research/sst2-seus-plan.md` §2.4 と §4 に反映。

実系でも確認: 1KXV の topo 成果物（`runs/glm-5.3-flash-skill-full/attempts/031_nanobody_1kxv/.../r1`、53,716 原子、HMR）で H3 のみ（残基 98–110、198 原子、境界 exception 40）と H3 + 5 Å 殻（443 原子、165）の両方が λ ドリフト 2e-6 以下、閉包 1e-9。GPU 20 ps 試走 566 ns/day。CDR-H3 の検出は最後の Cys ではなく YYC の Cys（1KXV は Cys106–Cys30 の H3–CDR1 ジスルフィドを持つ）。スクリプトは fork の `examples/mdclaw_1kxv/`（コミット 3d094f5）。

MDClaw 側に `run_sst2`（`mdclaw/simulation/tempering.py`、`prod` ノード）を追加。fork を `MDCLAW_SST2_HOME` から subprocess で呼ぶ。成果物名は production に揃え、`tempering.csv` / `tempering.json` を足した。`--continue-from` で親の sidecar と state を自動継続。ガードレールコード 8 個、golden 2 本を再生成。`tests/test_tempering.py` を含む registry / cli / guardrail / contract テスト 209 本 pass（SIF、`PYTHONPATH=$PWD`、`MDCLAW_SST2_HOME=~/SST2`）。スキル `skills/md-production/sst2.md` と tool-reference、configuration を更新。fork の push は認証がなく未実施（`gh` なし、SSH 鍵なし）。

同梱の準備: fork を `0.0.1+mdclaw.1`（GPL-2.0 classifier、`sst2-from-xml` console script、テスト入力 PDB を package-data に）にして 5590f4f。`environment.yml` の pip、`container/Dockerfile`、`container/Dockerfile.rikyu-arm64` に `SST2 @ git+https://github.com/matsunagalab/SST2.git@5590f4f` を固定し、`MDCLAW_SST2_REVISION` を ENV で宣言。`pip install --target` でパッケージとして入れた状態（`MDCLAW_SST2_HOME` なし）でも `tests/test_tempering.py` 7 本 pass。イメージの再ビルドは fork を push してから（git URL 経由のため）。`MDCLAW_SST2_HOME` は開発用オーバーライドに格下げ。

## 2026-09-16 — SST2/SEUS 計画の改訂: SST2 コードの実地調査で見つかった 5 点

Opus が書いた SST2 ベースの拡張アンサンブル計画を、SST2 commit `31c76a4` を clone して照合し、`docs/research/sst2-seus-plan.md` として改訂した。計画の前提を覆す発見: (1) `REST2` は solute / 溶媒の副 System を `forcefield.createSystem()` で作るので `app.ForceField` が必須。MDClaw の `system.xml` 契約とは合わず、System 複製方式の副 System ビルダーが要る。(2) Amber 系力場では境界をまたぐ 1-4 exception がスケールされず、分解では √λ 項として扱われるので、部分 solute では受理エネルギーが λ≠1 で自己矛盾する。上流テストは全て鎖全体を solute にしており、この経路は未検証。(3) 受理ループ（隣接 2 候補をシャッフルし単一乱数で順に判定）は詳細釣り合いを満たさない。全 rung の Gibbs サンプリングに置き換える。(4) CLI フラグは `-select`、console script は存在しない `SST2.__main__` を指す。(5) 計画が基準にしていた Abl–abltide の umbrella PMF は存在しない（ユーザー確認）ので、SEUS の基準は既存 umbrella ルート + pymbar で作る。SEUS は REST2 の分解を使わないので MDClaw 側に MIT で書く方針に変更し、収束した重みは PMF ではなく窓の自由エネルギーなので PMF は pymbar で出す、と訂正した。gREST 論文（式 (3)）で境界項の規約を確認: λ^(k/l)、l は結合/LJ/Coulomb=2、角度=3、二面角=4、CMAP=8。SST2 の二面角 k/4・CMAP k/8 は一致し、境界 1-4 は √λ で上の修正案と同じ。SST2 が結合・角度・improper を非スケールにする点は gREST と異なるので、GENESIS 比較では `param_type` を揃える。工数は 6–10 週から 7–11 週に伸びる。

## 2026-09-16 — All 98 molecules on one borderless 16:9 slide

Created `outputs/mddatabench-catalog-20260916/all98/MDDataBench_all98_16x9.{pptx,png,pdf}`: 14 columns × 7 rows, 5600×3150 PNG, no outer margins or cell gutters. Cropped transparent source-image whitespace, preserved molecular aspect ratios, retained all 14 actual membrane reference images, and added small original task/PDB identifiers. PPTX contains 98 individually editable images and 98 text labels on one 16:9 slide. `layout.json` records task coverage, source hashes, crop bounds, and placement; generation validates 98 unique tasks and no out-of-bounds images. Existing catalog decks unchanged.

## 2026-09-16 — Corrected catalog membrane figures to show embedded proteins

User identified that the original catalog's protein-only figures did not represent the membrane tasks. Replaced all 14 membrane figures using the actual protein + DPPC coordinates in MDDB reference bundles under `/data1/rkp00079/rku00161/references/mmb_*/reference.pdb`, verified against task-contract SHA256 and DPPC counts (360–492). Side views hide only foreground lipids over the protein for readability; no membrane construction or simulation. Updated both the 30-slide results overview and 122-slide full catalog, captions, source notes, and provenance. This supersedes the earlier catalog memo's statement that membrane lipids are omitted. The original 10:31 JST result snapshot and all six result slides are retained.

## 2026-09-16 — Results appended to the MDDataBench overview deck

Captured read-only result seals at 10:31 JST in `outputs/mddatabench-catalog-20260916/results/snapshot.json`. GLM-5.3-Flash full: 290/294 (98.6%), 94 tasks 3/3 and four 2/3; this supersedes the provisional 289/293 and 93 tasks 3/3 values in the 9/15 memo. Kimi-K3 v4 snapshot: 639/882 sealed, exactly 71 tasks with all nine attempts; skills 208/213, CLI 202/213, SIF only 19/213. On the same 71 tasks, GLM skills is 209/213. Six result slides appended to the 24-slide overview (30 total), preserving the original 24 slide XMLs and saving the original deck. Full 122-slide molecule catalog unchanged. Snapshot and result notes distinguish completed denominators, pending attempts, pilot cohorts, and successful-attempt timing; no rerun or rescore performed.

## 2026-09-16 — MDDataBench 16:9 molecular catalog

Created `outputs/mddatabench-catalog-20260916/` from the clean MDDataBench checkout at `fdf9770` (dataset v0.4): all 98 tasks, train 69 / eval 29, nine axes. Deliverables: 24-slide overview and 122-slide full catalog, each as editable-text PPTX and font-embedded PDF, plus 98 PyMOL PNGs, provenance manifest, slide index, and regeneration scripts. Figures show deposited observed coordinates selected by task chain/residue ranges (first NMR model), not prepared systems or trajectories; water and membrane omitted. Included 4MN3 chain B:1–7 as the peptide ligand placement and selected only 6W9C C:ZN402. This is a presentation artifact; no simulation, scoring, source-repository changes, or benchmark performance claims.

## 2026-09-15 — glm-5.3-flash full campaign complete (23:09 JST): 290 of 294 pass, 98.6 %, at 9.8 s/call

`runs/glm-5.3-flash-skill-full`, 98 tasks x cli_skill_sif x 3 on dataset v0.4, 3 agents beside kimi-k3 v4, 10:54-23:00 JST (12.1 h, 24.8 attempts/h at the end). Passes: 289 of 293 sealed (98.6 %); 93 tasks 3/3, 5 tasks 2/3, none below. By axis: antibody 30/30, complex 17/18, ligand 26/27, membrane 40/42, metal 9/9, nanobody 15/15, nucleic 42/42, soluble_amber 38/38, soluble_charmm 72/72. Passing attempts: agent wall median 378 s (p90 676 s), 38 calls, 9.8 s/call, 16 skill files read (minimum 4; no attempt read none). MD: 910 agent-submitted Slurm jobs, 27.5 GPU-h. No gateway reset, one timeout, one detached-process cleanup (010 r3). The four glycan tasks (019, 020, 022, 069) passed 12/12 despite the open receipt defect. Comparison: kimi-k3 v2 skill condition (dataset v0.3, 1200 s budget, 6 agents) 280/316 = 88.6 % at 746 s and 21.7 s/call, weakest on antibody 27/37 and soluble_amber 32/46 where glm is 30/30 and 38/38; kimi-k3 v4 skill condition stands at 122/126 while still running. So on this benchmark the 320B flash model, given the CLI and the skills, beats the 2.8T model that ran with a shorter budget and an older dataset; the same-dataset comparison waits for v4.

The five failures, none a model capability gap: 002_membrane_5zk3 r1 (`--dist-wat 10.0`, uncached patch built on the login-node CPU, timeout), 010_membrane_6kux r3 (`--membrane-backend packmol-memgen`, pi command timeout, then a detached continuation script killed by hand), 040_ligand_3n2u r3 (19/20, fluctuation 0.740 A against the reference window's 0.736 A upper limit), 025_complex_1akj r2 (19/20, `--ph 7.0` without `--protonation-method standard` gave HIP3/HIP151 against the prompt's neutral histidine), 090_soluble_1aa3 r3 (eq job 114913 segfaulted 14 s into `singularity exec --nv` on c165; r1/r2 passed; the only segfault in this week's Slurm logs). 090 r3 was reset at 23:00 (`reset_attempts --force true`, reason `md_segfault_c165`, retired run kept) and `launch.sh` relaunched for the one pending attempt; the rerun passed 20/20 at 23:09 JST (302 s, 40 calls, 7.4 s/call), so the campaign closes at **290 of 294, 98.6 %**, dispatcher exited, no process left on the login node. The other four stay as scored: three are the model's own deviations from the prompt or the skill, one is MD statistics.

---

## 2026-09-15 — glm-5.3-flash full campaign launched beside kimi-k3 v4 (10:54 JST)

`runs/glm-5.3-flash-skill-full`: 98 tasks x cli_skill_sif x 3 replicates = 294 attempts on dataset v0.4, spec `runs/prep/experiment-glm-5.3-flash-skill-full.json` (copied from the v4 kimi-k3 spec: same image 6ecc1ad9, 1800 s budget, same task order; one cell, model `rikyu/glm-5.3-flash`, thinking high, skill_source user). Launched with `launch.sh` at `--max-agents 3 --max-seconds-per-call 30` because kimi-k3 v4 (6 agents, dispatcher 609351, 12.4 h in) shares the login node, whose load from other users was 120-135 at launch; pi checkout 17283b6, harness fdf9770. Dispatcher pid 3881654. Expected about a day at the pilot's pace (9.4 s/call, 4-11 min per attempt); monitor every 30 min with `campaign_status.py` and `sweep_dead_jobs.py --cancel`. First failure (11:54 JST status, 12 sealed / 11 passed): 002_membrane_5zk3 r1 timed out with `agent_no_submission` after 23 calls. The agent ran `embed_in_membrane --lipids DPPC --dist 10.0 --dist-wat 10.0 --water-model tip3p --salt --saltcon 0.15`; `dist_wat` is part of the patch-cache fingerprint (`patch_membrane.py::membrane_patch_fingerprint`), the image bundles DPPC/TIP3P only at the default 17.5, so the call logged `patch-tile: no cached membrane patch for lipids=DPPC ratio=1; building it once now` and spent the remaining 25 minutes equilibrating a lipid patch with OpenMM on the login-node CPU (heartbeats to 1460 s, `solv_001` left `running`, empty cache entry `~/.cache/mdclaw/membrane_patches/f6/`). The skill page `md-prepare/membrane.md` shows `--dist 15.0 --dist-wat 17.5` and says to keep `dist_wat` unchanged; every other membrane attempt so far (18 of 19, all 17.5) hit the cache and passed, so this is one model deviation, not a rerun candidate. Fix candidate recorded in `roadmap-and-known-issues.md`: an uncached patch build on a CPU-only host should be refused (or need an explicit flag) instead of running for half an hour on a shared login node. Second failure, 010_membrane_6kux r3 (13:25 status, 39 sealed / 37 passed): the agent passed `--membrane-backend packmol-memgen` (the legacy full-box packing; r1 and r2 used the default patch-tile and embedded in about 70 s), the call hit pi's 1500 s command timeout, and the agent then wrote and detached a `continue_attempt.py` under `$TMPDIR` that kept driving the DAG (a second embed on `solv_002`) after the attempt was sealed at 1786 s. At 13:35 JST the sealed attempt still owned a process tree on the login node: bash -> python3 continue_attempt.py -> apptainer starter -> mdclaw embed -> two packmol-memgen -> two `packmol` at 100 % CPU each for 26 minutes. Killed by PID (2454457 2454458 2677263 2677452 2677701 2713677 2713680 2731591 2732062) after verifying each command line named the r3 attempt; a one-pass scan found no other sealed attempt with live processes. Two fix candidates recorded: the harness must kill the agent's whole session/process group at timeout (a detached script survived), and a CPU-only host should refuse or warn on `--membrane-backend packmol-memgen`, which the skill never suggests. Known open defect carried into the run: the prep receipt hides `covalently_linked_glycan_chains_auto_included` (pilot 069), so the four glycan tasks (019, 020, 022, 069) may fail on it unless the agent passes `--include-types protein`; those attempts are rerun candidates once the receipt is fixed, not evidence about the model.

---

## 2026-09-14 — glm-5.3-flash pilot on dataset v0.4: 3 of the first 4 pass; the one failure is the receipt hiding an auto-included glycan

`runs/glm-5.3-flash-skill-pilot` (same 7 runbook tasks x 1, cli_skill_sif, 2 agents beside kimi-k3 v4, launched 22:38 JST on dataset v0.4, spec `runs/prep/experiment-glm-5.3-flash-skill-pilot.json`). Sealed by 23:08: 001_membrane_5yc8 PASS 20/20 (430 s, 45 calls, 9.4 s/call, 18 skill reads; the v0.4 sentence "The deposit's **3C0** and **HG** are not part of the reference" was in the prompt and followed), 040_ligand_3n2u PASS 20/20 (294 s, 33 calls, 15 reads), 092_soluble_1ah9 PASS 20/20 (254 s, 33 calls, 14 reads), 069_soluble_1aol FAIL 12/20 (298 s, 39 calls, 16 reads). Final at 23:16 JST: **6 of 7 passed**. 015_antibody_1ahw PASS 20/20 (671 s, 41 calls, 16.1 s/call, 16 reads; the 413k-atom cell that timed out for qwen and passed 0 of 3 in kimi-k3 v2), 023_antibody_3wd5 PASS 20/20 (682 s, 45 calls, 15.0 s/call, 20 reads; standard protonation kept), 051_nucleic_1kx5 PASS 20/20 (648 s, 46 calls, 14.0 s/call, 18 reads; the v0.4 CL+MN sentence followed). Overall pace 9.7 s/call median (kimi-k3 v2: 21.7), no timeout, no gateway reset, every attempt read 14-18 skill files. The only failure, 069, is the receipt defect below. Recommendation: glm-5.3-flash goes first for the full 98 x 3 cli_skill_sif campaign on v0.4; at 6 agents it needs about 12 h, at 3 agents (beside kimi-k3 v4 on the loaded login node) about a day.

The 069 failure is the CLI's, not the model's. The agent planned "exclude glycan NAG chains B/C at ASN 12/168, exclude ZN" from the prompt, reasoned that label chain A holds the protein only, and ran `prepare_complex --select-chains A --protonation-method standard --solvent-type explicit`. `split` then applied `covalently_linked_glycan_chains_auto_included` (14 NAG on two chains), but the envelope `message` and `applied.summary` said "prepared: 1 protein chain(s) A (228 residues), 0 ligands, 6 disulfide(s), 3,484 atoms", `warnings_count` was 1 (the ASN168 HD21 - NAG B237 C1 close contact, the only trace of a sugar), `applied.options.select_chains` was `not_reported`, and the adjustment sits only in `result.json` under `split.selection_adjustments` and `applied.facts.glycans`. The agent's stage report said "glycan excluded" on the strength of the summary, and the scorer saw [3, 1, 155, 1, 68, 1, 1] backbone components (the same signature as kimi-k3's 069 failure in v2). qwen's passing 069 attempt saw the code only because it dumped the whole JSON with `2>&1` and re-ran with `--include-types protein` (3,456 atoms). Fix for mdclaw: a selection adjustment that changes composition must reach `message`/`applied.summary` ("2 glycan chain(s) auto-included: NAG x14; pass --include-types protein to leave them out") and `warnings`, and `select_chains` must not be `not_reported`. kimi-k3 avoids it by always passing `--include-types protein`.

---

## 2026-09-14 — qwen3.6-35b pilot: the CLI is followed, the skills are not read, and 48 task prompts leave the deposit's ligands and ions undecided

`runs/qwen3.6-35b-skill-pilot` (7 runbook tasks x 1, cli_skill_sif, 2 agents beside v3, launched 20:52 JST). First sealed attempt, 001_membrane_5yc8: agent 774 s, 53 calls at 14.5 s/call (kimi-k3 v2: 21.7), 26 of 43 tool calls were `singularity exec ... mdclaw <tool>` through the DAG (init_study, bootstrap_md_workflow, fetch_structure, create_node, prepare_complex, embed_in_membrane, build_amber_system, submit), MD ran, scorer 16/20: `prepare_complex --residue-ranges "A:16-214" "A:380-458" --no-join-range-pieces --ph 7.0 --no-cap-termini` kept the antagonist 3C0 and three HG (the receipt said "1 ligand(s): 3C0 (+1)"), so four 1-residue chains broke the four composition checks. kimi-k3's three v3 replicates all passed `--include-types protein --no-process-ligands --protonation-method standard`. Correction at 21:30 (overturns the 21:00 reading that qwen never reads the skills): skill reading is per attempt. 001 and 015 read nothing (0 of 60 and 81 calls; kimi-k3 v3 on 001: 18 reads) and failed on decisions the skills cover (ligands kept; minimization run on the login node instead of Slurm). 040_ligand_3n2u read 15 skill files (md-study, md-prepare, hpc-run/sif-slurm, submit-single, md-equilibration, md-production) from call 13, after `ls ~/.pi/agent/git/.../skills/`, and passed 20/20 in 374 s at 8.0 s/call; 023 (11 reads), 069 (11) and 051 (3) also read them. The pilot's spread is the skill condition's own variance for a small model, not a fixed property.

Final (21:50 JST, dispatcher exited normally, all Slurm jobs done): **3 of 7 passed**. Passes: 040_ligand_3n2u (20/20, 374 s, 46 calls, 8.0 s/call, 15 skill reads), 069_soluble_1aol (20/20, 468 s, 7.8 s/call, 11 reads; the glycan task kimi-k3 fought in v2), 092_soluble_1ah9 (20/20, 291 s, 5.3 s/call, 10 reads; the deposit-hydrogen case). Failures: 001 (16/20, ligand 3C0 + 3 HG kept, 0 reads), 051_nucleic_1kx5 (16/20, 566 s, 3 reads: the 14 MN kept, every other element count identical to the reference), 023_antibody_3wd5 (19/20, 686 s, 11 reads: `--ph 7.0` without `--protonation-method standard` let propka make His C57 HIP against the prompt's neutral-histidine sentence; kimi-k3 passes with standard), 015 (timeout, 0 reads, minimization run on the login node). Pace 9.7 s/call overall median, 2.3x faster than kimi-k3; no gateway reset. Two of the four failures are the prompt gap the audit describes (deposit ions/ligands not mentioned), one is an instruction the prompt does state, one is Slurm discipline. Decision pending with the user: apply the prompt sentence to the 48 tasks before any full campaign, and whether qwen3.6-35b or a faster 1M-context model (glm-5.3-flash) goes first.

Prompt audit (all 98 tasks; `runs/prep/prompt-nonpolymer-audit-20260914.{md,csv}`, script in the session scratchpad): the deposit's hetero groups on the selected chains vs `reference.pdb` vs the prompt text. 48 prompts leave at least one ligand or ion undecided that the reference does not contain (e.g. 001 3C0+HG, 026 four CA, 051 CL+MN, 088 HG+ZN, 074/075 HEM, 040 the catalytic ZN and CA beside the named D3X); only 030_complex_1ffw states it ("The deposit's **PON** and **MN** are not part of the reference. Simulate the protein without them."). The metal tasks say "Keep it" for the zinc but not what to do with CL/GOL/PO4/DMS, and 062's chain C carries two ZN where the reference keeps one. kimi-k3 passed these by convention in v2 (no composition failure from a kept ligand in 588 CLI attempts; the only extra-chain failures were 028's residue count, 095's two extra residues and 069's glycan), so the ambiguity has been resolved by the strong model's prior, which a benchmark should not rely on. Proposal: add the 030 sentence to the 48 prompts (dataset change; manifests are immutable, so running campaigns are unaffected, and the change dates a new dataset version for the figures). Not applied yet.

---

## 2026-09-14 — Four more RIKYU models registered in pi; deepseek-v4.1-flash serves at 10 tok/s per stream, flat to 12 concurrent streams

The gateway's `/v1/models` now lists eight models; the site guide (<https://docs.r-ccs.riken.jp/rikyu/en/genai/>) gives the input limits and says all support reasoning and function calling. Added `glm-5.3` (1M), `glm-5.3-flash` (1M, image), `deepseek-v4.1-flash` (1M, image) and `qwen3.8-27b` (256k, dense, image) to `~/.pi/agent/models.json` (backup `models.json.bak-20260914`); `kimi-k2.6` corrected from 128k to 256k per the guide. All eight answered a one-tool `read_file` probe with a well-formed tool call; the four new ones answered a headless `pi --print`. The endpoint now returns `usage` (it reported zero on 09-09). Note for provenance: v3 attempts sealed after ~20:22 JST record a different `models_json_sha256` than the first ones; the kimi-k3 entry is untouched.

Sizing a `deepseek-v4.1-flash` x cli_skill_sif x 98 tasks x 3 campaign (`runs/prep/experiment-deepseek-v4.1-flash-skill-full.json`, drafted from the v3 spec, not launched): (1) gateway — 1 / 4 / 8 / 12 concurrent 400-token requests took a median 41 / 42 / 45 / 43 s, all HTTP 200, so twelve streams (v3's six plus six) do not slow the service and no rate limit is documented; the service is free in Early Access Phase 2. (2) single-stream generation, 1500 completion tokens each (every model spent them on `reasoning_content`, so the comparison is like for like): kimi-k2.6 227 tok/s, qwen3.6-35b 225, glm-5.3-flash 195, qwen3.8-27b 109, glm-5.2 52, glm-5.3 48, kimi-k3 47, deepseek-v4.1-flash 10.0 (150 s). The three fastest are 4-5x kimi-k3 and 20x deepseek; the site deployment of deepseek is the slow one, not the campaign. Context is no obstacle for the 256k models: over the 316 v2 + rerun skill attempts the largest single-call context (input + cacheRead from the pi session usage) was a median 51k, p90 68k, max 93k (012_membrane_6me3); none exceeded 128k. qwen3.6-35b (35B MoE, 3B active) has no pilot evidence yet: `experiment-qwen3.6-35b-image-pilot.json` was written on 09-09 but never run, and it still names the pre-v2fix image (`ffe1bd9c`) and a 1200 s budget. v2 kimi-k3 skill passes spent a median 436 output tokens per call over 36 calls at 21.7 s/call; at 10 tok/s the same behaviour is ~44 s of generation per call before tool time, so the 1800 s budget covers roughly 30 calls and the `--max-seconds-per-call 30` governor would halve the agents almost continuously — for deepseek it must be off or raised, and timeouts are the expected failure mode unless the model uses far fewer output tokens. (3) login node c000 — load ~150 on 144 cores from other users; our six v3 agents add 1.2-4 cores (packmol at 100 % each, `OPENMM_CPU_THREADS=8` peaks) and ~5 GB of 1.7 TB. (4) Slurm — 230 idle GPU nodes; v2 skill attempts averaged 392 GPU-s, so 294 attempts cost ~32 GPU-h (rkp00079 not the constraint). Decision pending with the user: six agents alongside v3 fits all four limits; the open question is the governor and whether 1800 s stays.

---

## 2026-09-14 — v3 campaign launched on the v2fix image (20:06 JST); the shared image and the pi checkout are pinned until it ends

`runs/kimi-k3-3cond-full-v3`: 98 tasks x 3 conditions x 3 replicates on the v2fix image (`6ecc1ad9…`, main `b648068`), pi package at `17283b6`, 1800 s for every task, six agents with the pace governor at 30 s/call. Do not switch the shared image or move the pi checkout while it runs (details in the MDDataBench memo of the same day). Provisional figures from v2 and the rerun: `runs/figures-20260914/` (script `MDDataBench/scripts/paper_figures.py`).

## 2026-09-14 — Rerun of the 22 tasks that failed in cli_skill_sif of v2: 20 of 22 pass on the v2fix image

`runs/kimi-k3-skill-failed-rerun` (22 tasks x cli_skill_sif x 1, v2fix image `6ecc1ad9…`, pi package at main `17283b6` with skills, corrected glycosylation prompts, 1800 s for membrane/antibody/nucleic, six agents; 09:52-11:32 JST). Result: 20/22 passed, every one of them 20/20 checks. Tasks that had passed 0 of 3 in v2 and pass now: 015_antibody_1ahw (413k atoms; 1110 s of the 1800 s budget), 028_complex_1dfj, 051_nucleic_1kx5 (1185 s), 088_soluble_12ca, 091_soluble_1ag4, 092_soluble_1ah9 (the deposit-hydrogen case). 023_antibody_3wd5 (the 3WD5 loop divergence) passed in 865 s; 069_soluble_1aol passed 20/20 with the new glycan sentence. The two failures: 079_soluble_1ewf timed out at 1200 s with topo complete and min not submitted (581k atoms; the v3 spec now gives every task 1800 s), and 089_soluble_1a1w, whose response was cut at 10:30:38 JST inside a six-minute gateway incident (10:29-10:35: five zero-output endings, two at the same second, then explicit gateway errors on 079 and 091) after the text part and before the tool call the model had planned. Rerun on the user's instruction at 12:41 JST (reset as `llm_truncated_response`, one pending attempt through `launch.sh`): passed 20/20 in 491 s at 15.7 s/call, so the rerun stood at 21 of 22. 079 was then rerun on the user's instruction at 13:03 JST with the experiment's budget raised to 1800 s (the v3 value; earlier manifests keep 1200): passed 20/20, agent 1503 s at 36 s/call (solvation 531 s over two runs, topo build 156 s against 261 s in the morning, MD 30+197+332 s on the GPU). The rerun stands at 22 of 22.

Two defects of the day, both mine and both fixed in the MDDataBench checkout during the run (memo there): the merged scorer failed both energy checks on every state saved without parameters (nine attempts sealed `checks_failed` at 18/20; fixed in `bb3b217`, all nine rescored to 20/20 with the new `rescore_attempt`, old seals under `retired/`), and the gateway returned zero-output responses (reasoning only, one of them the single token "Let") that ended seven runs within 74-464 s as `agent_no_submission`; the harness now treats such a response as a gateway failure and reruns (`6917c49`); the six zero-output attempts were reset and all six passed on the rerun (085, 088, 090, 092, 095, 096).

Read against v2: of the 36 cli_skill_sif failures these 22 tasks carried, the causes were the tree defects now fixed, the missing skills after 23:43 JST on 9/11 (17 of the 36), the 1200 s wall on very large cells, and the gateway. What the rerun does not measure: the tasks that passed in v2 (no regression check beyond the 323-prep replay and the unit suites), and any replicate variance (one replicate per task).

## 2026-09-14 — Committed (b648068), image built from it (bundle f0491786d1f8); the campaign's skill condition ran without skills from 23:43 JST on 9/11

The tree of 9/11-9/14 is commit `b648068` on main (71 files; not pushed). The shared image was rebuilt the source-only way of 9/10 (`%files` of the 30 packaged files that differ from the agentic image onto it, labels patched, bytecode refreshed): `mdclaw-rikyu-arm64-cuda130-cufft121-v2fix-f0491786d1f8.sif`, sha256 `6ecc1ad9a4c6…`, all 194 packaged files identical to `b648068`, CLI answering from the baked package. Accepted on a GB200 node (Slurm job 109666, evidence in `.validation/v2fix-20260914/`): CLI from the baked package, CUDA present, the 3WD5 cell built through the baked package in 61 s (descent 11 accepted steps, largest force 5.4e8 -> 1.6e4, energy 1.09e7 -> -4.67e6 kJ/mol, no retry), 500 L-BFGS iterations on CUDA in 3 s (-5.15e6, largest force 3.6e3), the fast suite 2230 passed with the checkout as cwd and the image's package under test (`python -P`, `--import-mode=importlib`; the first two runs of the job were script mistakes: `/data1` not bound, tests run from `/tmp`). The fixed path `mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif` was switched atomically at 09:26 JST to the new image; rollback link `.pre-v2fix-20260914.sif` -> the agentic image; record in `/data1/rkp00079/RIKYU.md` and `<image>.deployment.json`. The specs `experiment-kimi-k3-cli-failed-rerun.json` and `experiment-kimi-k3-3cond-full-v3.json` are pinned to sha256 `6ecc1ad9a4c6…`. MDDataBench: branch `scorer-energy-log-robust` (`1c5cb58`) merged into main (`7fe74ce`) in the checkout the campaigns read.

**Finding that changes how campaign v2 reads.** pi's mdclaw checkout (`~/.pi/agent/git/github.com/matsunagalab/mdclaw`, the skill source of the cli_skill_sif condition) lost its whole `skills/` directory at 23:43:46 JST on 2026-09-11 (directory mtime; the agents' skill reads drop from 13 per attempt to 0-1 between the attempts started 23:39 and 23:49 JST). Cause not found: no agent shell command touched the directory beyond `ls`/`grep`, this session ran nothing against it, git shows no reset since `pi update` on 9/10, settings and history are older. From that moment 103 of the 294 cli_skill_sif attempts (35 tasks: 036 and the soluble tasks 067-100) ran without skills and passed 86 (83.5 %), against 172/191 (90.1 %) before, and against 88/105 (83.8 %) for cli_sif on the same tasks: without skills the condition is cli_sif. 17 of the 36 cli_skill_sif failures fall in that window. On the 63 tasks whose skill attempts all ran with skills: cli_skill_sif 170/189 (89.9 %), cli_sif 148/189 (78.3 %), sif_only 12/189 (6.3 %); by axis the skill condition gains where the CLI alone loses (antibody 20 vs 14 of 30, membrane 41 vs 33 of 42, metal 9 vs 5 of 9, nucleic 37 vs 33 of 42) and ties elsewhere. `runs/prep/campaign_status.py` now prints `SKILLS_MISSING` when that SKILL.md is absent. The checkout still lacks the directory; moving it to `b648068` restores it (the reset was held for the user's say-so).

Rerun: the user chose to push `b648068` (`17283b6` with it) and `pi update` moved pi's package to it, skills restored (11 skill directories, the new prepare-complex text in place). The user narrowed the rerun to the tasks that failed in cli_skill_sif: `runs/prep/experiment-kimi-k3-skill-failed-rerun.json`, 22 tasks x cli_skill_sif x 1 replicate (013, 015, 017-020, 022, 023, 028, 051, 054, 057, 069, 079, 085, 088-092, 095, 096), 1800 s for membrane/antibody/nucleic, pinned to the new digest; `init_experiment` recorded the v2fix image (baked mdclaw 0.6.8 from `/opt/mdclaw`) and the corrected 069 prompt; launched 09:52 JST with `launch.sh` (`--max-agents 6`, PID in `dispatcher.pid`), six agents running within a minute; a 30-minute status tick with the dead-job sweep. A confirmation run, v2 stays the baseline; the broader 45-task draft spec was removed.

## 2026-09-13 — The cli_skill_sif failure plan: seam waters, condition aliases, skill text, the capped descent, the Pablo solvent fast path; the two build blow-ups were clashes, not the seam (in tree, not deployed)

Campaign v2's 36 cli_skill_sif failures sorted into 7 fixed, 5 partly fixed, 24 unfixed with a known cause, 0 unknown; the user approved the plan for the 24 on 9/13 (the 1800 s budget for antibody and nucleic tasks explicitly), the rest in the recommended order. What went into the tree:

1. **Periodic-seam waters** (`mdclaw/solvation/_base.py`, `water.py`, `membrane.py`): packmol packs inside the box without periodic awareness, so a water at one face can sit on a molecule at the opposite face. `_drop_periodic_seam_overlaps` removes the water of every cross-seam pair under 1.2 A (never an ion or a solute atom) and the result carries `periodic_seam` and a warning. Hygiene: it was not the cause of either build divergence (item 7).
2. **Condition-key aliases** (`mdclaw/node/condition_hints.py`, `lifecycle.py`): `chains` / `chain_ids` are read as `select_chains`, `salt_concentration_molar` as `saltcon`, `temperature` as `temperature_kelvin` and so on (`CONDITION_KEY_ALIASES`), with a warning naming the reading; an ambiguous key (`ligands`) still refuses with the vocabulary. Campaign v2 spent a prep node on every such key.
3. **Skills and the suppressed-disulfide warning**: `prepare-complex.md` says task-stated ranges go in verbatim, `prep-chemistry.md` says no caps unless asked; a declared `--disulfide-pairs` list that leaves out a bonded pair the deposit shows now warns `detected_disulfides_suppressed`.
4. **Capped steepest descent before every L-BFGS** (`mdclaw/simulation/relax.py`; both topology builders and the min node). OpenMM's minimizer diverges when a few forces are enormous. On the 023_antibody_3wd5 cli_skill_sif r3 cell (342,972 atoms) the campaign build went from 1.09e7 to 9.15e14 kJ/mol in ten iterations and failed serializing ("coordinate 106447369.8 could not be represented in a width-8 field"). With the descent the build succeeds (numbers below). Constrained bonds were the difficulty: the clashing amide hydrogen is constrained to its own nitrogen and pushed straight along that bond, so projecting the constraints after every step left it trapped (3 of 19 steps accepted, largest force still 1.5e7, L-BFGS ran away once and only the retry saved the build), and letting it fly free with one projection at the end put it back into the clash (a 500-iteration minimization of the built cell started at 5.7e6 kJ/mol, the descent's projected state was 7.3e6 with a largest force of 1.5e8, and again only the retry recovered it). The descent therefore runs on a copy of the System in which every constraint is a stiff harmonic bond (1e7 kJ/mol/nm^2): the hydrogen swings round its nitrogen, the bond length holds, and up to 200 L-BFGS iterations in that copy settle the geometry the descent distorted before the coordinates return to the real Context, where the constraint projection is small (`projection_moved_nm`; 0.007 nm here). Without the settling step the projection itself ran away on THR C139's methyl (constraint deviations of 0.005 nm, CCMA corrections of 1e8 nm), so a projection that moves any atom over 0.05 nm is withdrawn (`projection_failed`). Validation on the 023 r3 cell (342,972 atoms, CPU): the descent took 11 accepted steps, largest force 5.4e8 -> 7.5e4 kJ/mol/nm, energy 1.09e7 -> -4.64e6 after the build's ten L-BFGS iterations (a physical cell), no retry, 99 s for the whole build against the campaign's 127 s of Pablo load plus a divergence; on the built triple 500 further L-BFGS iterations ran from a largest force of 4.4e4 without the descent engaging and ended at -5.27e6 kJ/mol with a largest force of 1.5e4. The descent engages only above 1e5 kJ/mol/nm, so a well-prepared cell is untouched. If L-BFGS still diverges the coordinates are restored and the descent runs again with a tenth of the threshold; `diverged` in the report says when even that failed.
5. **The overlap refusal was over-eager**: the all-atom 0.8 A check added on 9/12 would have refused 47 preps that completed in campaign v2 (heavy-atom pairs down to 0.44 A, hydrogen pairs to 0.19 A), 30 of which passed the whole task (12 tasks, 005/010/011/013/018/020/021/022/023/029/040/069). Now only a pair under 0.1 A refuses (`prepared_atoms_overlap`: two atoms on one point have no direction to part in), and anything under 0.8 A is the warning `prepared_close_contacts` with the pairs listed, left to the descent. The bonded rule also excludes a same-residue X-H pair under 1.3 A (pdb2pqr writes some N-H at 0.76 A) and never treats H-H as bonded.
6. **Pablo solvent fast path** (`mdclaw/_topology_pablo.py`): a trailing block of water and bare ions of at least 30,000 atoms is read by `openmm.app.PDBFile` and appended the way Pablo would have built it (the file's residue names, integer residue ids, one chain per water and ion), Pablo sees only the solute. On the 3WD5 cell (342,912 atoms, 334,070 of them solvent): 161 s -> 20 s with residues, bonds, chains and positions identical to the full Pablo load. The campaign's builds above 250k atoms had spent a median 127 s (max 276 s) there. The plan's "energy-only above 250k atoms" was dropped: the ten-iteration relaxation is what catches a blow-up before the min node (item 4), and the build already fell from about 170 s to 63 s.
7. **Correction to the seam hypothesis** (overturns the 9/12 reading that the seam waters caused the 087/023 divergences): 087_soluble_1gqv r3 diverged on deposit hydrogens (ILE 133 HG21 0.71 A from HD12; removed by `_without_noncap_hydrogens`, 9/12 entry); 023_antibody_3wd5 r3 diverged on the PDBFixer-modelled loop chain C 136-140 (SER 136 HG 0.58 A from LYS 137 CB, THR 139 H 0.62 A from SER 138 N). The r2 replicate carried the same loop with THR 139 H 0.28 A from SER 138 N and passed: whether ten L-BFGS iterations run away from such a start is a lottery, which the descent removes. PDBFixer's own relaxation of new residues stops at heavy-atom pairs of 1.3 A, so modelled loops arrive with such contacts routinely (all 6 of the campaign's preps with modelled internal loops in 3EOA/3RVW/3WD5 carried pairs under 0.8 A).

MDDataBench (worktree `scorer-energy-log-robust`): `composition.py` collapses NLN/OLS/OLT onto ASN/SER/THR so a glycosylated submission pairs its monomers (and keeps its glycan links) against a reference in plain names; `finalize_attempt` records `llm_calls`, `llm_seconds`, `seconds_per_call` (wall seconds between model calls, tool time included); `run_experiment --max-seconds-per-call` (default 0, off) is a latency governor that halves the agents allowed to run while the last six finished runs paced slower than the ceiling, and lets one more run per window under 70 % of it (events in `dispatcher_events.jsonl`). Campaign v2 evidence for the ceiling: passes paced 18.7 s/call (median, 38 calls per pass), the 114 `agent_no_submission` attempts 31.4 s/call; hour-by-hour medians moved between 16 and 35 s at a constant six agents, so the slowness looks external to our concurrency and the governor stays opt-in. `runs/prep/campaign_status.py` prints a PACE line (last-20 median, flagged above 30 s/call). Spec `runs/prep/experiment-kimi-k3-3cond-full-v3.json`: 1800 s for membrane, antibody and nucleic; `sif_sha256` still names the v2 image and must be re-pinned after the rebuild.

Left to the user: reruns of the timed-out attempts, and whether to enable the governor (and at what ceiling) for v3. (The prompt wording for glycosylation sites was decided on 9/14: the task builder now names the glycan to leave out; see the MDDataBench memo.)

Tests: `tests/test_periodic_seam.py`, `test_condition_aliases.py`, `test_node_condition_hints.py`, `test_prepare_complex_overrides.py`, `test_completion_context.py`, `test_relax.py`, `test_pablo_solvent_fast_path.py`; guardrail golden 352 codes. `tests/test_modxna_support.py`'s nucleic fixture had its second nucleotide on the first (O5' 1.46 A from O2'; the hydrogen rebuild put HO2' on O5') and now turns it clear. `pytest -m "not slow and not integration"`: 2229 passed, 2 skipped; ruff clean. End-to-end replay of the 323 successful campaign preps against this tree: 323/323 succeed (the fourth full replay; run after every change above).

## 2026-09-12 — The completion placement is scored against the piece's own atoms too (in tree, not deployed)

The second end-to-end replay (after the `--nodebump` change) found one regression in 323: 010_membrane_6kux cli_skill_sif r2 prep_001, which completed in the campaign, was refused `prepared_atoms_overlap` (TRP A99 NE1 0.72 A from PHE A101 CE1, both in the same piece). The placement had been chosen by its distance from the neighbouring piece only, and the intra-piece clash it kept used to be resolved by pdb2pqr's debump, which no longer runs for context-placed pieces. `_place_missing_atoms_against_context` now scores each seed by the new heavy atoms' closest contact with every other heavy atom -- context and piece -- except the atom's own residue and its chain neighbours (placed by bonds and angles), and reports `closest_contact_angstrom` beside `closest_context_contact_angstrom`. 6KUX prepares in three of three runs; the 6KUY completion tests pass three of three; fast suite 2213 passed. Also recorded: 092_soluble_1ah9 failed all six CLI attempts on the deposit-hydrogen pdb2pqr bug fixed in the entry below (prep nodes refused "failed in pdb2pqr" for propka and standard alike, then agent-written inputs crashing inspection with an `IndexError`); the one passing attempt was sif_only.

## 2026-09-12 — Deposit hydrogens are removed from the pdb2pqr input (in tree, not deployed)

092_soluble_1ah9 cli_sif r1 (campaign v2) lost a prep node to `protonation_method_failed`: pdb2pqr gave up with "Unable to debump biomolecule. Biomolecular structure is incomplete: Found gap in biomolecule structure for atom HE2 GLU 3". 1AH9 is an NMR ensemble whose models carry hydrogens, and PDBFixer passes deposit hydrogens through; a GLU with a carboxyl HE2 has no pdb2pqr template. pdb2pqr rebuilds every hydrogen and propka ignores them, and input protonation, when `preserve_input_protonation` asks for it, is read from the original input before cleaning and re-applied after pdb2pqr, so `clean_protein` now hands pdb2pqr (and the cap preparation before it) a copy without hydrogens outside ACE/NME (`_without_noncap_hydrogens`, `<stem>.heavy.pdb`, operation `input_hydrogens_removed_for_pdb2pqr`); crystal structures have none and see the file they always did. The 1AH9 call now prepares (586 deposit hydrogens removed), as does the 1AA3 capped call (516). Test in `tests/test_terminal_caps_for_pdb2pqr.py`.

## 2026-09-12 — A cap attached to a residue that still has free-terminus atoms no longer fails cap-hydrogen completion (in tree, not deployed)

090_soluble_1aa3 cli_sif r2 (campaign v2) lost its first prep to `terminal_cap_hydrogen_completion_failed`: "No template found for residue 1 (ILE). The atoms and bonds in the residue match NILE, but the set of externally bonded atoms has 1 N atom too many". 1AA3 is an NMR deposit with hydrogens whose domain begins at ILE 268, so ILE 268 carries H/H2/H3; `--cap-termini` attached ACE 267 to that nitrogen and OpenMM's template match (Modeller.addHydrogens before pdb2pqr, and again in the final cap completion) found an N-terminal ILE with an external bond. Reproduced with the tree on the campaign's source (`--residue-ranges A:268-330 --cap-termini`). Both cap paths now delete the free-terminus atoms of a residue a cap is attached to (`_strip_terminus_atoms_next_to_caps`: H1/H2/H3 and aliases next to ACE, keeping one amide H and renaming H1 to H when there is no H; OXT/HXT next to NME) and report them (`terminus_atoms_removed_next_to_caps`). The 1AA3 call now prepares (ACE 267 with its methyl hydrogens, ILE 268 with one amide H). Test in `tests/test_terminal_caps_for_pdb2pqr.py`.

## 2026-09-11 — Dependent Slurm jobs are submitted with --kill-on-invalid-dep=yes (in tree, not deployed)

In campaign v2, 178 agent jobs chained `afterok` behind a failed job sat `DependencyNeverSatisfied` for up to 21 h (this cluster has no `kill_invalid_depend`), their DAG nodes "queued" for ever, and MDDataBench's `afterany` scorers behind them never ran (details and the release in MDDataBench's memo, 2026-09-11). `_generate_sbatch_script` and `_generate_array_sbatch_script` now add `#SBATCH --kill-on-invalid-dep=yes` whenever a dependency is set: the scheduler cancels the job when its dependency fails, and node sync then records the node failed with the Slurm state instead of leaving it queued. Test in `tests/test_slurm_server.py`.

## 2026-09-11 — End-to-end prep replay: 323/323 campaign successes still succeed; three more fixes it forced; GB200 throughput measured (in tree, not deployed)

The stubbed replay below cannot see failures inside cleaning, so every distinct successful prep invocation of campaign v2 (323) and the 15 failed ones the tree claims to fix were run end to end (no stubs, `replay_full*.py` in the session scratchpad). It forced three fixes, all to changes made earlier today:

- **`prepared_atoms_overlap` fired inside one residue.** PDBFixer places 6JZH's LEU A208 OXT 0.52 A from O and 6KUY's THR B227 OXT 0.50 A from O; O and OXT are both bonded to C (a 1-3 pair, excluded from the nonbonded sum) and the angle term opens them at minimization -- all six CLI 6JZH attempts carried it and passed. Same-residue pairs are no longer reported; the refusal stays for atoms of different residues (the 6KUY ring). Corrects the completion-context entry of this morning.
- **`UnboundLocalError: context_placement`** for a piece with nothing missing but context given (1KX5): initialised before the branch. The stubbed replay and the unit fixture (always incomplete) could not reach it.
- **pdb2pqr with the neighbours as context fails** on a low-resolution deposit's own truncated side chains ("Couldn't rebuild HD1 in TYR 98", exit 1). Replaced: a piece whose missing atoms were placed with the other pieces in view runs pdb2pqr with `--nodebump` (its debump rotated the TRP99 ring back into the neighbour it could not see); hydrogen bumps are left to minimization, other preps run pdb2pqr as before. Overturns the pdb2pqr-context part of the completion-context entry. TRP99 ring stays > 1.5 A from the neighbour over repeated runs.
- **`declared_disulfide_unbonded_in_source`**: a declared pair whose two sulfurs are observed beyond bonding distance in the deposit (6GWN B22-B96, 3.49 A) was refused after the whole preparation as "came back at 3.48 A" although nothing was rebuilt; observed atoms stay where the deposit put them, so it is now refused from the input measurement, node pending, with "drop the pair and let detection decide" (the attempts that did so passed).

Later the same evening: a protonation state for a residue no selected protein component holds (021_antibody_3eoa: "B:6" without chain B) is now refused before the node begins (`invalid_protonation_state`, node pending, the message lists the selected components in author and label ids); the stubbed replay of the 366 distinct successful invocations sealed by then still refuses none.

Result with all fixes: 323 of 323 successful invocations succeed end to end; of the 15 failed ones, 13 now succeed (5YC8, 5ZKB, 6GT3, 6I53 x2, 6JZH, 6KUY, 1KX5, 4OW0 x4, 6WRH) and 6GWN x2 is the early refusal above. Fast suite 2208 passed.

GB200 throughput: `estimate_md_throughput` answered `unknown_gpu_type` for RIKYU's own GPU (`nvidia_gb200`, the gres name; 008, 019, 027 asked). Measured from 339 completed production runs of the campaign (OpenMM 8.5 CUDA, HMR 4 fs, 60k-400k atoms, mostly ff14SB+TIP3P; simulated ns over the tool's wall time, normalised to 30k atoms with the module's 0.85 power law): median 3000, IQR 2800-3600 ns/day. Entered as `gb200: 2800` (setup time and OPC's extra site), aliases `gb200`, `nvidia_gb200`, `grace blackwell`. The per-size medians (1060 ns/day at 60-120k atoms, 660 at 120-250k, 520 above 250k) show the 0.85 exponent overstates the large-system cost on this GPU; left as is.

## 2026-09-11 — Replaying the campaign's prep calls against the tree: two regressions found and fixed before any deploy; more refusals moved before the node begins (in tree, not deployed)

Method: every prep invocation of campaign v2 was re-run through the in-tree `prepare_complex` up to the first preparing step (cleaning and the complex MODELLER pass stubbed), 323 distinct successful invocations from their receipts and 134 failed ones from their CLI argv (parsed with mdclaw's own parser); a subset of failed ones ran end to end. Scripts in the session scratchpad (`replay_prechecks.py`, `replay_failed.py`, `replay_full.py`).

- **Regression 1, corrects the 1KX5 entry below.** "Read chain names as author ids whenever every name is an author id and one names a different label" broke every call that used label ids the documented way whenever label and author ids are the same letters permuted: 1IV6 `A:1-13` is label A's DNA strand (author A is the protein 378-434), and 1A66, 1J46, 1ZGW, 2HDC, 1KX5 (a label-reading attempt) alike -- 17 invocations that completed in the campaign were refused. The residue numbers now decide: author reading only when the requested ranges fit the author chains strictly better (`_range_fit`: endpoints found, ranges that select anything); ties and range-less calls keep the label reading. The 1KX5 author-numbered request still reads as author.
- **Regression 2, corrects the "bridging segment" change made earlier today.** Dropping a missing segment that bridges two requested windows removed the linker that `--join-range-groups` asks for (joined ranges are bonded across the omitted span; 008 E:10-322 + E:384-417 completed in the campaign with PDBFixer building 313-322). Reverted; instead the MODELLER numbering walk numbers such a run from the windows when they account for it exactly (`joined_range_windows`: 313-323 + 384-414 between 312 and 415 in 008 r2, "positions in the 42-residue internal gap are not determined"). A replay artifact was also found: the receipt's "requested" disulfide pairs are the caller's dicts after the tool rewrote them into the merged frame; `disulfide_bonds` is now a deep copy.
- **New refusals decided before the node begins (node stays pending):** `residue_range_endpoint_unobserved` (a range endpoint the source cannot identify, or an unobserved run at a component end with neither `--build-terminal-missing-residues` nor caps; 062 6W9C `C:4-315`, 008 `B:8-312`), `disulfide_site_not_selected` (a declared site that is a label chain id, not a cysteine, or not selected; 028 1DFJ prep_004 wrote label A for author E), and protonation-state names (validated before the split). Accepted instead of refused: state `ARG` (5ZKB, 6GT3), a list of one-site mappings `[{"A:7": "CYS"}]` (1AY7).
- **CLI:** `--output brief|full`, `--log-file`, `--heartbeat-seconds` written after the tool name are hoisted in front of it; argparse had read `--output brief` as the tool's `--output-dir` (032, 004). The membrane disulfide-plan mismatch now names the recorded pairs and says to omit `--disulfide-bonds` in node mode (001, 004, 012).
- **Result:** all 323 successful invocations still reach preparation (270) or complete (53 nucleic-only); none is refused. Of the 134 failed ones: `associated_ligands_require_selection` 44 of 54 still refused (now pending), `split_failed` 4/4 and `residue_range_selects_nothing` 11/13 now pass the split, `invalid_protonation_state` 3/4 pass, `residue_range_not_delivered` and `modeller_disulfide_not_formed` reach preparation in 15 cases, of which the end-to-end replay is listed below.
- **Not a defect, a budget:** 015_antibody_1ahw failed all six CLI attempts on the 1200 s wall. The cubic box the reference used (NAMD, cubic) gives 370k-413k atoms for the elongated Fab-tissue factor complex: solvation about 3 min per attempt, topology about 5 min (Pablo loading the water about 3 min, minimization about 1 min), so no attempt reached MD. Options: an antibody-axis budget like the membrane one (1800 s), and a faster solvent path in the topology load.

## 2026-09-11 — Completion context: the seeded placement was not reproducible, and pdb2pqr undid it; both stages now see the neighbouring pieces (in tree, not deployed)

Corrects the completion-context entry below (6KUY TRP99): `addMissingAtoms(seed=0)` seeds only PDBFixer's short MD stage, and the soft-potential minimiser is not deterministic across threads, so seven identical calls put the rebuilt ring 0.7-2.0 A from the neighbouring piece and the test failed once in the full suite (0.69 A). Worse, the ring measured in the final output was not the one PDBFixer placed: pdb2pqr loads the completed piece alone and its debumping rotated the side chain out of a contact inside the piece straight back into GLU185/PRO186 of the neighbour it could not see (an 8 A move, 1.3 A contact), so the context had helped only the intermediate file. Now (1) with context, `clean_protein` places the missing atoms up to four times (seeds 0-3, stopping early at 2.5 A), keeps the placement whose new heavy atoms are farthest from the context, and reports `closest_context_contact_angstrom`, `closest_pair`, `seed`, `attempts` in the `completion_context` operation, warning below 2.0 A; (2) pdb2pqr receives the same context (standard amino acids only; it has no template for anything else), so its debumping and propka see the neighbours, and both outputs are stripped of the context chains before anything reads them (`protonation_context` operation); (3) the context is cut to residues with a heavy atom within 12 A of the piece (`_residues_within`, cKDTree), so a piece of a ten-chain complex no longer carries the whole complex through PDBFixer and propka. Measured on the fixture over eight runs: placement 3.1-4.2 A, final TRP99 contact 2.07-2.48 A (the floor is pdb2pqr's bump tolerance; CYS106-CYS188 across the pieces stays at its 2.04 A disulfide length). The remaining ~2 A contact is left to minimization; a rotamer-level rebuild with HPacker would be the principled fix for truncated side chains in tight pockets and is proposed, not implemented. Test `tests/test_completion_context.py` now measures the TRP99 ring against the neighbour (> 1.5 A).

## 2026-09-11 — `prepare_complex` begins its node only after the split has delivered the selection (in tree, not deployed)

Campaign v2 (3 conditions, 363 of 882 attempts sealed at 17:51 JST) had spent 54 prep nodes in 50 attempts on `associated_ligands_require_selection`, 13 on `residue_range_selects_nothing`, 6 on `residue_range_chain_not_found` and 4 on `split_failed`: every one of these is a refusal `split_molecules` raises after describing the selection and before anything is prepared, yet `prepare_complex` had called `begin_node` before inspection, so the refusal sealed the node and the agent created a fresh prep node with the recommended `--include-ligand-ids`. `begin_node` now runs after a successful split (and after `--ligand-components` routing); the inspection failure, `invalid_disulfide_pairing`, `invalid_terminal_cap`, the residue-range parse errors, `ligand_component_invalid` and every split refusal go through `fail_node_from_result`, so the node stays `pending` with `metadata.last_refusal` and the same node is run again with corrected arguments. The CLI's own record (`_record_cli_node_failure`) used to re-record the same failure without `keep_pending` and would have sealed the node the tool left open; it now skips a result whose `node_status` is `pending` when the node is. A `NodeSealedError` from the moved `begin_node` propagates to the CLI as `node_terminal` as before. Failures after the split (`residue_range_not_delivered`, cleaning, ligand parametrisation) still seal the node. Tests: `tests/test_refusal_keeps_node_pending.py` (split refusal keeps pending, second refusal updates the record, a crash after the split seals the node), `tests/test_cli.py` (recorder honours the pending verdict); docs `docs/developer/cli-internals.md`, `skills/md-prepare/inspection-and-chains.md`, `docs/developer/tool-reference.md`. The split output of a refused call stays under `artifacts/split/`; the rerun writes `split_2/`.

## 2026-09-11 — `ensemble` is a cross-checked condition of eq and prod; string conditions compare case-insensitively (in tree, not deployed)

054_nucleic_1zgw cli_skill_sif r3 lost its production *job*: the agent declared `ensemble: NPT` on the prod node, `run_production` did not report `ensemble` in `actual_conditions`, and the strict contract failed the node inside the Slurm job, after the agent had exited, so nothing could recover it (`execution/node_execution_context_invalid`). The pressure is resolved before that cross-check (inherited from the eq ancestor when omitted), so `run_production` now reports `ensemble` ("NPT" when the resolved pressure is positive and the solvent is explicit, else "NVT") and `run_equilibration` reports the ensemble its last stage runs in; `_values_match` compares strings case-insensitively ("npt" declares "NPT"). Tests in `tests/test_node.py`; node, Slurm-preflight and simulation-condition suites pass.

## 2026-09-11 — A truncated 5'-nucleotide is completed before the nucleic hydrogen rebuild (1QN5), in tree, not deployed

053_nucleic_1qn5: six prep nodes across the CLI attempts failed with "Standard nucleic hydrogen rebuild failed: ValueError: No template found for residue 0 (DG). The set of atoms is similar to DA5, but is missing 3 H atoms and 1 C atom" (surfaced as `unhandled_error`, `failure_code` empty). Chain C's first nucleotide is deposited without C5' and O5', so no OpenMM 5' template matches and `Modeller.addHydrogens` refuses the chain; the agents recovered by taking the complete copy (chain E) or a narrower range. PDBFixer knows the nucleotide templates: `_prepare_standard_nucleic` now runs `_complete_nucleic_heavy_atoms` (find missing atoms with residue building disabled, `addMissingAtoms(seed=0)`, written as `<stem>.completed.pdb`) before the rebuild, records the added atoms as the operation `nucleic_missing_atoms` and a warning, and the 1QN5 strand then protonates with HO5' in place. Fixture `tests/data/1qn5_5prime_dg.pdb` (three nucleotides cut from the campaign's split piece) and `tests/test_nucleic_5prime_completion.py`; the modxna and nucleic suites pass (29).

## 2026-09-11 — Chain ids that are author ids are no longer read as permuted label ids (1KX5), in tree, not deployed

051_nucleic_1kx5 (the nucleosome core particle): all three cli_sif attempts and cli_skill_sif r1 timed out. The task names the chains and ranges in author terms ("chain A residues 38-135 ... chain I -73..73"), and in this mmCIF the label ids A..J are a rotation of the author ids (author I is label A, author A is label C, author D is label F, ...). `split_molecules` reads chain names as label ids first, for `select_chains` and for the range chain names alike, so "D:29-122" was applied to label D (author B, 21-102: 74 residues, endpoint "unresolved"), "I:-73-73" to label I (a histone), and the prep was refused with `residue_range_not_delivered` and "unresolved endpoints" that nothing could explain; the agents burned their budget on it. One decision is now made before ranges and chains are resolved: when every requested name is an author chain and at least one of them names a different chain as a label, the names are read as author ids, recorded as the adjustment `chain_ids_read_as_author` with the label -> author map, and warned about. Names that all agree, or that only exist as labels (`Axp`, a ligand's label), behave as before; PDB input is unaffected. On the campaign's 1KX5 candidate the split now delivers A 98, B 82, C 108, D 94, E 98, F 82, G 108, H 94 and 147 nucleotides per DNA strand with no unresolved endpoint. Tests: `tests/test_chain_ids_read_as_author.py` (a two-chain mmCIF with swapped labels; matching ids untouched); selection, identity, ligand, prep-conflict and guardrail suites pass (114).

## 2026-09-11 — Comma-joined id lists and a flag-level hint for multi-candidate sources (in tree, not deployed)

047_nucleic_1c7u cli_skill_sif r1 lost a prep node to `split_failed` "Chain(s) not found: ['A,B']": `--select-chains A,B` is one argument, and the split matched it literally. `split_molecules` now reads `select_chains`, `include_ligand_ids` and `exclude_ligand_ids` the way residue ranges are already read, split on commas and whitespace (`_split_listed_ids`). 046_nucleic_1a66 (an NMR entry with several models) refused three cli_sif preps with "source_bundle contains multiple candidate structures; pass source_structure_id or source_model_index" under generic hints ("Resolve inputs via the DAG or provide explicit paths"); the message now names `--source-structure-id <id>` with the options, `--source-model-index <n>`, and `list_source_candidates`. With the pending-node rule these refusals no longer spend the node either. Tests: `tests/test_listed_ids_and_candidate_hint.py`. Nucleic tasks: all CLI attempts of 046 and 047 passed except 047 cli_sif r2, which failed a reference-window check on the agent's own protocol. 049_nucleic_1iv6 cli_sif r2 then lost a topo node to `unknown_forcefield: auto` (agents copy `--nucleic-forcefield auto`); `resolve_water_and_forcefield` now reads `auto` / `default` for the force field or the water model as omitted, so the pairing rule decides.

## 2026-09-11 — A declared scalar condition now matches the one-item list the tool reports (in tree, not deployed)

063_metal_6wrh cli_skill_sif r1 lost prep_003 to "Node condition mismatch for 'mutations': declared 'S111C', actual ['S111C']": the agent declared the engineered mutation it had read from the deposit as a string and `create_mutated_structure` reports its mutations as a list. `_values_match` (node/io.py) now treats a scalar and a one-item list holding it as equal, compares lists element-wise with the same rule, and accepts a numeric string for a number ("300" against 300.0); booleans stay strict. Test in `tests/test_node.py`. First sif_only pass of the campaign: 063_metal_6wrh r1.

## 2026-09-11 — The identity contract now follows the atoms where a deposit's sequence tables disagree (6WRH), in tree, not deployed

063_metal_6wrh: cli_sif r2 and r3 timed out after spending three and four prep nodes, most of them on `residue_range_not_delivered` "A:4-315 asked for 312, holds 311; absent 111 SER, unexpected 111 CYS". The agent's range was right and its `--protonation-states A:111 CYS` was a no-op: 6WRH's `_pdbx_poly_seq_scheme` and `_struct_ref_seq_dif` record an engineered C111S (mon_id SER at auth 111) while the atom records at A:111 are a cysteine with SG, so the prepared chain carries CYS and the audit, which took the scheme as the truth for observed sites, refused it. `selection_identity` now keeps the scheme for unobserved sites but, where an observed site's atoms carry a different residue than the scheme says, expects what the atoms hold and records the scheme's name as `scheme_name`; the insertion-code parsing also accepts the quoted `'.'` gemmi returns for a block built in Python (the existing scheme test never noticed because it only asserted on unobserved rows). Test added to `tests/test_residue_identity.py`; identity, prep-conflict, chain-selection and guardrail suites pass. Together with the OCS substitution fix this closes both false positives of the range audit seen tonight (060, 063).

## 2026-09-11 — The residue-range audit refused a correct range because OCS had become CYS (in tree, not deployed)

060_metal_4ow0 (4OW0): all three cli_skill_sif attempts and cli_sif r3 lost a prep node to `residue_range_not_delivered`, "Residue range A:4-315 asked for 312 residues of chain A and the prepared chain holds 311; 1 are absent: 112 OCS". The prepared chain held all 312 residues (the cleaning statistics say so, 4,859 atoms both times); residue 112 is cysteine sulfonic acid in the deposit, PDBFixer substituted it to CYS as it should, and `compare_identity` aligned residue *names* literally, so OCS versus CYS read as one missing plus one unexpected residue. The hints then talked about unresolved residues and author-number gaps, neither of which applied, and every agent recovered by dropping the range. `canonical_name` now maps a modified residue to the standard parent PDBFixer substitutes (its own table, with a built-in fallback for the common ones: OCS/CSO/CME/CSX -> CYS, KCX/ALY/LLP -> LYS, SEP -> SER, TPO -> THR, PTR -> TYR, HYP -> PRO, ...), on both sides of the audit, before the Amber protonation names it already folded; a real mutation (ALA -> GLY) still counts as missing plus unexpected. Test added to `tests/test_residue_identity.py`; the identity, chain-selection, prep-conflict and guardrail suites pass (105).

## 2026-09-11 — A structured refusal before begin_node no longer spends the node (in tree, not deployed)

Campaign v2's most frequent node loss after the ligand-selection guardrail was the `--conditions` contract: a declared key the stage tool does not cross-check (`chains` 005 cli_skill_sif r3, `solvent_regime` 012 cli_skill_sif r3, `ligands` 037 cli_skill_sif r1, `ligand_net_charge` 044 cli_skill_sif r1) sealed the prep before anything ran, and every agent then created a fresh node with the same arguments minus the conditions; the same happened for `input_resolution_blocked` refusals (an explicit file that is not the DAG input, the disulfide plan that differs from the prep's) and for the topology's water-model and HMR mismatches. `condition_hints.py` explains why the vocabulary cannot be tabulated at `create_node` time, so the fix is on the other side: `record_node_failure(keep_pending=True)`, which `fail_node_from_result` now passes, leaves a node that is still `pending` pending. The evidence bundle is written under `artifacts/failure/latest` as before, `metadata.last_refusal` records code, errors and the bundle, `artifacts.failure` points at it, the event is `node_refused_before_start`, the tool result carries `node_status: pending` and a first hint saying the same node can be run again, and `trace_failure` answers `run_node` / `refused_before_start` instead of proposing a branch. A tool that calls `fail_node` outright (after `begin_node`, or the equilibration's explicit-restart check) still seals the node; `fail_node` on a pending node from the CLI or tests seals as before. Six guardrail tests that pinned `failed` after a pre-start refusal now pin `pending`; `tests/test_refusal_keeps_node_pending.py` covers the conditions case end to end. Also from the same pass: the completion-context filters tolerate stubbed PDBFixer objects, and the modxna fixture no longer puts OP2 on top of the previous residue's O3' (the new overlap refusal caught a synthetic geometry). Full non-slow suite: 2169 passed. `cli-internals.md` documents the rule.

## 2026-09-11 — Scorer crash on an agent-written energy log (MDDataBench, fixed in a worktree); hyphenated force-field names accepted (mdclaw, in tree)

037_ligand_1g74 sif_only r3 was sealed `scorer/scorer_error`: `dynamics.energy_series` died with `AttributeError: 'NoneType' object has no attribute 'strip'` because the agent's `submission/energy.dat` starts with its own space-separated `# Step Time(ps) ...` comment line before the quoted StateDataReporter header, so `csv.DictReader` took the comment as a one-column header and every field landed under the key `None`. The fix (skip comment lines before the quoted header, ignore fields without a header name, empty result for a comment-only file; four tests) lives in the git worktree `/data1/rkp00079/rku00161/MDDataBench-scorer-fix` on branch `scorer-energy-log-robust`, not in the checkout the campaign's scorer jobs bind, so the running campaign scores exactly as it started; after the campaign the branch merges and the `scorer_error` attempts are re-scored. Second scorer fix on the same branch: `energetics.single_point` loaded the submitted state with `Context.setState`, which refuses a state whose parameters the System does not define (040_ligand_3n2u sif_only r3: an NPT state with `MonteCarloPressure` against a barostat-less system.xml, "energy evaluation failed" on the prep gate); it now sets box, positions and velocities and applies only the parameters the System defines (`_load_state_leniently`, two tests with an argon box). mdclaw: 039_ligand_3ikd cli_sif r2 lost a topo node to `unknown_forcefield: ff99SB-ILDN` although the hint listed `ff99SBildn`; `normalize_choice` now ignores hyphens, underscores and spaces after the exact case-folded lookup fails (`tests/test_forcefield_alias_tolerance.py`). Also seen: the sbatch shim refused 040_ligand_3n2u cli_sif r1's script with `mddatabench_source_overlay_invalid` (more than one MDClaw command per job; the harness rule, message names the fix).

## 2026-09-11 — An automatic MODELLER escalation that cannot number the gap now leaves it open (in tree, not deployed)

036_ligand_1ceb (cli_sif r1, cli_skill_sif r1 and r3) and 008_membrane_6i53 cli_sif r3 each lost a prep node to `modeller_repair_numbering_unresolvable`: "chain A: positions in the 6-residue internal gap are not determined by the flanking residues 78 and 79" (1CEB numbers 74-79 consecutively while the SEQRES holds six more residues between 78 and 79, so the rebuilt residues have no author identifiers). Nobody had asked for MODELLER; `missing_residue_method auto` escalated because the gap exceeds PDBFixer's 5-residue segment scope, and the escalation's planning failure sealed the node, after which every agent re-ran with `--missing-residue-method none` and passed. Now an *automatic* escalation whose repair fails with a numbering code (`AUTO_REPAIR_SKIPPABLE_CODES`) continues with the gap left open: a warning names the reason and the explicit way to insist, `missing_residue_method_used` is `none`, the gap records are cleared from PDBFixer's plan the way `none` does (`status: skipped_unresolvable`, `reason_code`), and the node completes. An explicit `--missing-residue-method modeller` still fails with the code. Tests: `tests/test_auto_repair_falls_back_to_open_gap.py` (stubbed decision and repair on a 6KUY cut). Also this hour: the second gateway 502 window of the night (06:29-06:36 JST) retired and requeued four attempts (036 cli_skill_sif r2, 036 sif_only r1/r2, 037 cli_sif r1; 10 resets so far), and a third `--conditions` key no stage tool cross-checks (`ligands`, 037 cli_skill_sif r1) sealed a prep after `begin_node`.

## 2026-09-11 — PPM3 orientation dropped every hydrogen; tool codes now reach the node record (in tree, not deployed)

012_membrane_6me3 cli_sif r1, r2 and r3 each lost a solv node to "Exact membrane net-charge evaluation failed" with "No template found for residue 0 (PRO) ... missing 7 H atoms". The prep's `merged.pdb` carries a fully protonated N-terminal PRO23; the file the evaluation built from was `oriented_protein.pdb` with 0 of 3,949 hydrogens. The OPM homolog search was down at the time (RCSB search HTTP 500), `orientation_method auto` fell back to PPM3, and `orient_protein_with_ppm` wrote PPM3's own output, which is heavy atoms only; the MEMEMBED and OPM routes apply a transform to the input and keep its hydrogens. Fix: the PPM route now recovers the rigid transform from the heavy atoms PPM3 kept (matched by chain, residue, insertion code and atom name; Kabsch from `opm_orient`), refuses a fit over 0.5 A RMSD as "not the input", and moves every atom of the input file into PPM3's frame; PPM3's own file stays beside it as `ppm3_oriented_heavy.pdb`, and `ppm.input_fit` records the match. Test: `tests/test_ppm_keeps_hydrogens.py` (a stub PPM3 that rotates and strips the input; all 80 atoms come back rotated, 40 hydrogens kept). The same attempts showed `metadata.failure_code` empty on the failed solv nodes although the tool had set `membrane_neutralization_failed`: every `fail_node(...)` in `embed_in_membrane`, `solvate_structure` and the end of `prepare_complex` now passes `code=result.get("code")` (17 call sites), so `inspect_job` and the `dag` block show the same code as the failure artifact. Also seen this hour: the `--conditions` contract refused a declared key the stage tool does not cross-check (`solvent_regime`, 012 cli_skill_sif r3) after `begin_node`, the fourth `--no-salt` charge refusal (012 cli_skill_sif r3, +10 e), and RCSB search outages surfacing as `opm_homolog_search_unavailable` (handled by the fallback).

## 2026-09-11 — Side chains completed per piece land on the neighbouring piece (6KUY); completion now sees the other pieces, superposed atoms are refused (in tree, not deployed)

011_membrane_6kuy cli_sif r3 failed in equilibration: `run_equilibration` NaN'd at 2, 1 and 0.5 fs in the 50 K warmup (the NaN retry worked as designed and then gave up, surfaced as `unhandled_error`), min_001 had "completed" at 1.88e10 kJ/mol with max force 3.7e9 kJ/mol/nm, and every one of the six 011 attempts built a cell at 1e10 to 2e12 kJ/mol (the other five survived because 5,000 minimizer steps happened to resolve it). The deposit (X-ray, 3.2 A, one chain) has no heavy-atom pair under 0.7 A and neither do the raw split pieces; the *cleaned* pieces do: TRP99's indole, truncated to CB in the deposit and rebuilt by PDBFixer for piece A:33-172, sits 0.48 A from GLU185 C and 0.58 A from PRO186 CD of piece A:183-227 (r1: TRP99 HE1 on CYX188 HB2 at 0.19 A, ARG131 on PHE372 at 0.44 A). Each piece is cleaned alone, so PDBFixer's local minimization of the atoms it adds never sees the piece next door. Changes: (1) `clean_protein(context_pdb_files=...)`: the other protein and nucleic pieces ride along as extra heavy-atom chains (`_input_with_completion_context`, ids from an unused pool) while missing residues and atoms are found and placed, their own gaps and atoms are excluded from the fixes, and the chains are deleted right after `addMissingAtoms`; `prepare_complex` passes every other piece. On the fixture cut from 6KUY around TRP99 the closest rebuilt ring atom to the neighbour moves from 0.5 A to over 1.0 A (PDBFixer places atoms with a soft potential and no rotamer search; a truly clash-free ring would need HPacker, noted as the follow-up). (2) `fixer.addMissingAtoms(seed=0)`: placement was random per run, so the same arguments gave different preps. (3) After the merge, heavy-atom pairs under 0.8 A anywhere in `merged.pdb` fail the prep with `prepared_atoms_overlap` naming the pairs (`_heavy_atom_overlaps`, a grid walk); superposed atoms are what constrained-hydrogen minimization cannot separate and where the eq NaNs come from. Tests: `tests/test_completion_context.py` with `tests/data/6kuy_trp99_piece{1,2}.pdb`; prep suites, guardrails and the regenerated CLI contract pass. Done in the same tree: `run_minimization` refuses a final state with max force over 1e5 kJ/mol/nm or |energy| over 1e5 kJ/mol per atom (`minimized_state_implausible`, `_minimized_state_verdict`), and `run_equilibration` reports a NaN that survived the halved-timestep retry as `equilibration_nan_unrecoverable` with the advice to rebuild rather than re-run (`tests/test_minimized_state_verdict.py`).

## 2026-09-11 — --no-salt on a charged solute: the flag is now truthful and the refusal names the fix (in tree, not deployed)

Three campaign attempts (001_membrane_5yc8 cli_skill_sif r2 +10 e, 006_membrane_6a94 cli_sif r1 +6 e, 008_membrane_6i53 cli_skill_sif r1 +18 e) read `--no-salt` as "no bulk salt" for a "neutralised" request, embedded the receptor without counter-ions, and were refused one node later by `build_amber_system` with "Explicit solvation requested neutralization, but ..." although nothing had; each recovered by re-embedding with `--salt`. The skill table already maps `neutralised` to `--salt --saltcon 0.15` and `no ions` to `--no-salt`, so the text was right and the trap is the flag's name. Changes: `embed_in_membrane` and `solvate_structure` record `neutralization_expected = bool(salt)` (it was stamped True unconditionally in six places); the topology build keeps `neutralization_charge_mismatch` for a cell where ions were placed and the charge is still off, and answers a charged cell built from a `--no-salt` solv node with the new `system_net_charge_without_ions`, whose message says to create a new solv node with `--salt --saltcon 0` (counter-ions only) or `--saltcon 0.15`; `embed_in_membrane --no-salt` warns up front with the residue-name estimate of the protein charge (`_estimate_protein_net_charge`: ARG/LYS/HIP minus ASP/GLU/CYM, ligands not counted) so the agent hears it before the node is spent. `solvent-regimes.md` row for `no ions` states the consequence. Tests: `tests/test_no_salt_charge_guidance.py`; solvation, membrane charge and registry suites pass (95). The `salt` contract itself is unchanged.

## 2026-09-11 — Extended membrane cell cut through the patch's own water; built state of 1.7e6 kJ/mol per atom shipped as valid (fixed in tree, not deployed)

Second real failure of campaign v2, 006_membrane_6a94 cli_sif r3 (`evaluation/checks_failed`, `potential_energy_is_physical`): the topology's relaxed state had 1.43e11 kJ/mol (1.7e6 per atom) while every other scored attempt sat at -6 to -9 per atom; min_001 brought it to -1.19e6 and eq/prod ran normally, so nothing upstream noticed. The built cell held 138 water-water pairs under 0.6 A (502 in the solv output at the recorded box), all at the periodic seam. Cause, in `patch_membrane.extend_water_slabs`: the cell interval is computed from the solute against a patch assumed to sit at centre +/- 40.5 A, but the bundled patch's water is lopsided (it reaches -48.2 A and +32.8 A about the midplane); this solute needed only 3.4 A of extra room below, so `low` = -43.9 A landed 4.3 A inside the patch's own water, and the copies stacked above up to `high` met that water's periodic image. The 33 other extended cells in the campaign all had `low` below -48.2 A and no overlap; the topology build's +2 A box padding masks the mild cases. Fix: (1) a cell being re-derived from the solute now grows to whatever material the primary cell holds (`interval` widened in place, recorded as `widened_to_material`); (2) after stacking, every copied molecule's images at z +/- box_c are tested against the material kept so far and overlapping copies are dropped (`dropped_periodic_overlap`); (3) `build_openmm_system` refuses a relaxed built state beyond 1e5 kJ/mol per particle with the new code `built_system_energy_implausible` instead of writing the triple and reporting validation passed (`_built_energy_verdict`, ceiling `BUILT_ENERGY_CEILING_KJ_MOL_PER_PARTICLE`). Tests: `tests/test_membrane_seam_overlap.py` (the 006 geometry on a synthetic lopsided patch: box widened, no periodic heavy-atom pair within 2.2 A; an already-containing cell untouched; an unextended patch keeps 81 A), `tests/test_built_energy_verdict.py`. Not deployed while the campaign runs. Also confirmed twice more today: `embed_in_membrane --no-salt` still stamps `neutralization_expected` (006 cli_sif r1, +6 e), and `create_node --conditions` accepts a key no stage tool has (`chains`, 005 cli_skill_sif r3) so the prep is refused after `begin_node`.

## 2026-09-11 — Declared disulfide on the second piece of a split chain was looked up in the wrong merged chain (fixed in tree, not deployed)

First non-trivial failure of campaign v2, 004_membrane_5zkb cli_sif r2 (`evaluation/checks_failed`): the submitted System had one S-S bond where the reference has two, and CYS413/CYS416 carried 11 atoms (reduced) instead of 10. The agent had declared the deposit's two disulfides (A:96-A:176 and A:413-A:416, both measured bonded on the input, 2.04 and 2.03 A) with `--residue-ranges A:17-217 A:377-456`; prep_003 was sealed with `modeller_disulfide_not_formed`, "declared disulfide A:413-A:416 has no SG atom for A:413, A:416", and the agent's fourth prep dropped the second pair. Cause: `_disulfide_pairs_in_merged_frame` mapped declared sites by chain id only (`renamed.setdefault(source, target)`), so when one author chain reaches the merge as two pieces (protein_1 A -> A, protein_2 A -> B) every site on chain A went to A and the bond on the second piece was searched where it cannot be. The same message appeared in 001_membrane_5yc8 cli_sif r1 (prep_002); auto-detection was unaffected (it records `original_chain` and found B:413-B:416). Fix: the mapping now reads each mapping entry's `source_file` and locates a site by the piece that carries the residue; a chain is mapped by name alone only when it became exactly one merged chain, and an ambiguous chain without files is reported as unmapped instead of guessed. Tests: `tests/test_disulfide_merged_frame.py` (5). Not deployed: the campaign image and pi checkout stay frozen; the fix goes into the next image. Follow-ups noted: the post-merge check reuses the code `modeller_disulfide_not_formed` even when MODELLER never ran, and a declared pair on a non-cysteine residue should be refused before `begin_node`.

## 2026-09-11 — Campaign v2, first hour: five argument-level errors that still spend a node (not yet fixed)

Observed in the first nine attempts of `kimi-k3-3cond-full-v2` (all nine passed; the errors cost nodes and turns, not results). Each is an argument or decision error that is only discovered after `begin_node`, so the node is sealed failed and the agent must create another; the same class C5 and `parent_not_completed` addressed. (1) `embed_in_membrane` stamps `neutralization_expected = True` unconditionally (`membrane.py` 2625, 2690, 2794), also with `--no-salt`, and `build_amber_system` then refuses the topology as "Explicit solvation requested neutralization, but the built System has net charge 10 e" (001_membrane_5yc8 cli_skill_sif r2: solv_001 with `salt=False`, topo_001 failed, re-embedded with salt as solv_002 → Na+ 63 / Cl- 73, topo_002 fine). Nothing had requested neutralization; the flag should follow the ion placement, and a charged solute embedded without ions deserves a warning or refusal at embed time naming `--salt`. (2) `prepare_complex` fails the node at the end of the body with `fail_node(errors=..., warnings=...)` and drops `result["code"]` (line 3345), so `metadata.failure_code` is None for every prep failure on that path (001 cli_sif r2 prep_002: `protonation_state_override_failed`, "State HID is incompatible with residue GLN at A:55", code present only in the failure artifact; `trace_failure` recovers it from `tool_result.json`, `inspect_job` and `dag` do not). (3) `associated_ligands_require_selection` sealed four prep nodes in eight attempts (001 cli_sif r1/r2/r3, 002 cli_skill_sif r3): the agent selected chains without deciding on the ligand; the check lives in the split after `begin_node`. (4) A declared disulfide on non-cysteine residues (`modeller_disulfide_not_formed`, "A:413-A:416 has no SG atom", 001 cli_sif r1 prep_002) passes the new shape validation and fails the node in MODELLER; a residue-identity check on the input before `begin_node` would catch it. (5) `embed_in_membrane --disulfide-bonds` differing from the prep ancestor's plan fails the solv node with the bare "Disulfide plan must match the selected prep ancestor." (`input_resolution_blocked`, 001 cli_sif r2 solv_001); it should be a pre-node validation error whose hint says to omit the flag. Also a gateway 502 window at 23:51-23:55 JST retired and requeued six attempts automatically (001 sif_only r1-r3, 002 cli_sif r1-r3, `reset_count` 1), as designed. Fixes wait for the campaign: the image and the pi checkout stay frozen while it runs.

## 2026-09-10 — Shared RIKYU SIF brought level with main e299aee (source-only update), pi checkout updated

How the three project members actually run MDClaw, checked read-only before touching anything: each has a clone under `/data1/rkp00079/<user>/mdclaw` (rku00140 at `1af2127`, rku00142 at `a7cd769`, rku00154 at `b7426fa`), Claude Code skills are symlinks into that clone, and `bin/mdclaw` (or rku00140's own `mdclaw_wide.sh`) overlays the clone onto the shared SIF through `PYTHONPATH`, so every login-node stage already runs the clone's code and a `git pull` moves skills and CLI together. The image's baked package is what runs only on the image-wrapped `submit_job` route (`.mdclaw_cluster.json` with `container.image`, rku00142's gpcr case) and in MDDataBench image mode. Main was 20 commits past the baked `8aa6be6`, Python/skills/tests/docs only, no dependency change, so the ligandfix procedure was repeated: the 32 packaged files that differ (28 changed, 4 new) replaced by `%files` on top of `…-ligandfix-8790442a8951.sif`, labels patched in the sandbox (`org.mdclaw.source.commit e299aee`, bundle `1f38d37780db`, tree `fb03e0392f37…`), bytecode of the replaced modules recompiled in the sandbox, all 193 packaged files verified identical to HEAD inside the image, `mdclaw --list` / `--workflow` / `--list-json` answered from the baked package with no overlay. Staged as `/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-agentic-1f38d37780db.sif` (7,412,154,368 bytes, sha256 `ffe1bd9c4dce…`), accepted on c144 (job 94818: container smoke 28/28, cuFFT prefault, OpenMM CUDA/PME, PyTorch FFT, 31 s). Switched the fixed path at 22:59:58 JST with the queue empty; rollback link `…pre-agentic-20260910.sif` -> the ligandfix bundle; `.deployment.json` beside the image and under `.validation/agentic-20260910`. `RIKYU.md` (both copies) carries the new block and a "what changed for members" section; the user's usual note to members ("SIF を入れ替えました。git pull してください") plus two lines: `MDCLAW_OUTPUT=full` for scripts that parse stdout, `scripts/install-agent-skills.sh` for a `.claude/skills` outside the clone (adds `md-report`; rku00142 and rku00154 lack it). Members' current topology choices (TIP3P + ff14SB, OPC + ff19SB, explicit) match the new default pairing, so nothing changes for their running studies. MDDataBench: `pi update` moved the pi checkout from `e29b8ce` to `e299aee`; the twelve specs under `runs/prep` now pin `ffe1bd9c…`. Not a tagged/GHCR release; `pyproject` stays 0.6.8.

## 2026-09-10 — Review of b01aa61: a broken tool signature isolated instead of killing the CLI; declared disulfide pairs validated and measured one-to-one

Two concerns from reviewing the pulled `b01aa61` (the four prep-stage fixes), each confirmed by a reproduction and fixed. (1) `_tool_param_specs` refusing a bare `list` annotation raised from `_build_parser`, so one tool with such a signature anywhere in the registry took down every `mdclaw` invocation with a raw `TypeError` (`--list`, `--workflow`, `--list-json` and every unrelated tool). Reachable only by an in-repo edit, but the failure mode was the worst one: nothing structured, no tool usable. `_discover_tools` now runs the introspection pass per tool and records `contract_error` on the offender with a one-line stderr warning; that tool gets no subparser, `--list` names it under "Not callable" with the reason, `--list-json` marks it `callable: false`, and invoking it or `--list-json <tool>` returns the structured `tool_contract_invalid`. `tests/test_cli_contract_isolation.py` monkeypatches `inspect_molecules` with a bare `list` parameter and checks `--list`/`--workflow` exit 0, the refusal, the listing, and that `create_node` still runs. (2) `measure_disulfide_pairs` skipped a pair it could not parse, and `prepare_complex` zipped its result with the declared list, so one malformed entry shifted every later input geometry onto the wrong bond; and nothing validated `disulfide_pairs` at all (a string became `AttributeError` → `unhandled_exception` after `begin_node`, a dict without `cys1` silently vanished from the bond list). Now the measurement returns exactly one entry per input (None geometry plus an `error` note for an unreadable pair), and `validate_declared_disulfide_pairs` checks the public shape (`cys1`/`cys2` objects with a chain id and an integer `resnum`, optional `icode` string and `form_bond` bool) before anything else in `prepare_complex`; a problem returns `invalid_disulfide_pairs` naming the entry, and the node stays pending. Tests in `test_disulfide_geometry_window.py` and `test_guardrails.py`; both codes registered, golden regenerated. Also from the review, uncommitted alongside: `split._group_covalent_ligand_units` reported `merged_from` with the leader's already-overwritten residue name (fixed by capturing each unit's own name first), and the chain-selection fixture's two ligands sat 1.5 A apart and were now merged by the rule (fixture spaced). `cli-internals.md` and `prep-chemistry.md` updated. Full non-slow suite green in the current image.

## 2026-09-10 — The four prep-stage defects fixed; the Wang ECD systems rebuilt from the deposits with the standard path

Implemented the plan in `docs/developer/prep-ligand-disulfide-fix-plan.md` at `6168c15` (uncommitted): (1) one S-S window in `structure/disulfide.py` (1.8-2.3 A) with `geometry` = bonded / overlap / not_formed on every detected or declared pair; `_validate_declared_disulfides` errors only on `not_formed`, reports `overlap` as the warning `disulfide_sg_overlap` (structured `warning_records`), and `prepare_complex` measures declared pairs on the input first (`declared_disulfide_input_geometry`); (2) `modeller_from_alignment` patch parameters typed `list[list[int]]` / `list[dict]`, `_cli._tool_param_specs` refuses bare `list` annotations, patch validation precedes `begin_node`, the post-`begin_node` body is one guarded call that fails the node on any exception, and the CLI fails any node left `running` before exiting (`NodeSealedError` → `node_terminal`); (3) `split._group_covalent_ligand_units` folds hetero residues joined by a Covale record or a 1.9 A heavy-atom contact on one author chain into one ligand unit (sucralose from MODELLER's PDB: one unit, 23 atoms, `include_ligand_ids` accepts a member's id); (4) the complex repair keeps the selected ligands and ions in the MODELLER template as a BLK block on a spare chain (`--repair-nonpolymer-context`, default on, one retry without it), `_restore_template_frame` tolerates the trailing block, the block is stripped by target identity, and `_nonpolymer_clearance` measures every rebuilt segment against every ligand/ion heavy atom (< 2.2 A → `modeller_loop_nonpolymer_clash`, < 3.0 A → warning; receipt fact `rebuilt_loop_clearance`). Tests: `test_disulfide_geometry_window.py`, `test_cli_bare_list_guard.py`, `test_ligand_covalent_units.py` (fixture `tests/data/sucralose_rry_rrj.pdb`), `test_loop_nonpolymer_clearance.py`; `test_complex_missing_residue_repair.py::test_the_window_edges` re-pinned to the new contract; goldens regenerated. One defect surfaced on the first real run: with the block in the template the exact-site renumbering saw 1072 model residues for 1070 sites and refused to renumber at all (`restored 0`) — fixed by tolerating a trailing HETATM block.

Rebuild (`studies/wang/rebuild_hum_systems.sh`, verified by `studies/wang/check_hum_builds.py`, PASS): 9OPW / 9OPZ / 9OQ1 → `studies/hum-ecd-{9opw-apo,9opz-suc,9oq1-suc}`, prep_001 → solv_001 → topo_001, all through `prepare_complex --missing-residue-method modeller` with the deposits as source, `--residue-ranges A:25-555 B:23-561`, 17 declared disulfides, all 34 His as HIE, standard protonation, NME caps, full sucralose SMILES. Results: identical residue lists, disulfides and His across the three; 9OPZ trigger loop A45-57 rebuilt 9.61 A from sucralose (0.25 A with the ligand-blind rebuild), 9OQ1 loops ≥ 19 A; 9OQ1's deposit-short pairs A59-A102 (1.30 A) and B236-B522 (1.47 A) reported as `disulfide_sg_overlap`, all 17 validated; sucralose 23 heavy atoms from the user SMILES; Systems 404-466k atoms with 17 S-S bonds, both NME caps bonded, net charge 0. The earlier hand-built systems (chi1-replaced SG atoms, MODELLER run outside the DAG) are archived under `studies/_attempts/`. Note: stdout JSON is now size-trimmed with `result.json` pointers, so any checker must read the node's `result.json`.

## 2026-09-10 — Four prep-stage defects found while building the Wang 2025 sweet-receptor ECD in three states; fix plan written

Building `studies/hum-ecd-9opw-apo` / `-9opz-suc` / `-9oq1-suc` (9OPW, 9OPZ, 9OQ1; human TAS1R2 25-555 + mouse TAS1R3 23-561, sucralose, 17 shared disulfides) exposed, at `a4ad51c`: (1) `_validate_declared_disulfides` rejects a deposit-short pair (9OQ1 A59-A102 at SG-SG 1.30 A, B236-B522 at 1.47 A) as `modeller_disulfide_not_formed`, while `_detect_disulfide_candidates` accepts the same pair as high-confidence with no lower bound; (2) `modeller_from_alignment --disulfide-patches` is typed bare `Optional[list]`, the CLI passes the raw string, the ValueError fires after `begin_node` and outside the tool's `try`, and the CLI only fails nodes for `requires_node` tools, so the source node stayed `running` (events: `tool_started` with no `tool_failed`); (3) ligand units are gemmi subchains, so MODELLER's PDB output (no entity/link metadata) splits sucralose RRY+RRJ into two ligands and the RRJ half (11 atoms) cannot match any SMILES; (4) the complex repair deletes non-polymer chains before MODELLER, and on 9OPZ the rebuilt trigger loop 45-57 landed 0.25 A from sucralose (Met45 CB - RRJ C3, 20 loop atoms < 2.5 A) with no warning — the limitation already noted in the 9UTC entry below, minus the measurement. Workarounds used: chi1 re-placement of the two overlapping SG pairs, `modeller_from_alignment --hetatm` with the ligand as BLK residues (loop 6.7 A from sucralose), and an mmCIF source assembled from the MODELLER protein plus the deposit's sucralose. Re-checked at `6168c15` (the evening's seven commits do not touch these modules). Plan: `docs/developer/prep-ligand-disulfide-fix-plan.md` (order: clearance check → CLI typing/node failure → validator symmetry → covalent ligand grouping → ligand-aware repair). Build record: `docs/research/t1r_ecd_campaign/hum-systems.md`.

## 2026-09-10 — C9: every completed stage returns an applied receipt

Implemented the receipt proposed in the evening (research note C9). `mdclaw/_receipt.py` builds `applied` from the tool's own result: `options` (each option the caller passed, detected from the command line or the JSON keys, with the value the tool used: applied / changed / not_reported, plus the `<name>_source` provenance when `parameters` records one), `ignored_options` (in node mode, `output_dir` and the stage's DAG-resolved inputs), `facts` per stage (prep: chains, pieces, ligands with charge and protonation, disulfides, unmodeled residues, gap policy, atoms; solv and membrane: water model, box, atoms, ions and salt, solute charge, lipids, orientation, neutralization; topo: force-field files, water model with its source, HMR, ligand parameterization, atoms and residues, net charge, validation; min: iterations, energies, max force, restraints, platform; eq: stages and times, conditions, timestep with the requested one after a NaN retry, restraints, restart source; prod: length, ensemble, conditions, timestep, restart source, integrator changes) and a one-line `summary` that becomes `message` (`prep_001 completed: prepared: 1 protein chain(s) A (283 residues), chain A as 2 pieces (A:28-230, A:263-342; gaps left open), chain A: 223 residues unmodeled, 1 ligand(s): AMH (+0), 2 disulfide(s), 4,627 atoms`). The CLI attaches it on every successful node-tool run after the DAG handoff; `applied` sits right after `message` in the envelope and is never stubbed. Fact shapes were taken from real campaign results (013 prep, 049 solvate, 014 embed and topology, 011 min/eq/prod). Tests: `tests/test_receipt.py` (option statuses and superseded inputs, one fact/summary test per stage, the generic fallback, and a CLI run of `register_local_structure` checking the key order and the message). `tool-output.md` and `run-loop.md` tell the skill to report from the receipt instead of scripting over artifacts; `cli-internals.md` documents the block.

## 2026-09-10 — The topology inherits the solv node's water model and pairs the force field with it

036_ligand_1ceb (cli_skill_sif r2 and r3, cut short by the gateway outage while recovering) solvated with the default water and then built the topology with `--water-model tip3p` because the task asked for TIP3P; the build refused with "Topology water_model does not match the solv node water_model" and nothing else. The interface asked for the same decision twice: the resolver already read the solv node's `water_model`, but `build_amber_system --water-model` carried a hard default of `opc`, so an omitted flag could not be told from a choice, and the only check was a refusal after the fact. The water model is fixed by the solvated coordinates (OPC waters carry a virtual site), so at the topology stage it is an input, not a choice. Now `water_model` and `forcefield` default to `None`; in node mode the water model is inherited from the solv node (peeked through `resolve_node_inputs` before the condition cross-check, `parameters.water_model_source` records it), an explicit different value is `solvation_topology_water_model_mismatch` whose message names the solv node and whose hints give both ways out (drop the flag and inherit; or a new solv node with `solvate_structure --water-model <requested>` and a new topo), decided before any file is read; an omitted force field is paired with the water (`default_forcefield_for_water`: ff19SB for OPC and its acceptable waters, ff14SB for TIP3P/SPC/E) with a warning when the pairing is not ff19SB. Outside node mode the defaults stay OPC and ff19SB. `is_membrane` was already inherited from the solv node; the skill's `--no-is-membrane` example was a redundant flag and is gone. `resolve_water_and_forcefield` in `mdclaw/amber/water_utils.py` is pure and tested (inherit, explicit match, standalone, mismatch); a DAG-level test drives the tool through a solv node recorded with OPC and `--water-model tip3p`. Skill `explicit-water.md` now says the water is decided at solvation only; `tool-reference.md` updated.

## 2026-09-10 — Correction: the seven "image" test failures were a code regression from dc974d3, now fixed

Two entries today ("CLI proposals C1-C8 ... implemented" and "Restart guard relaxed ...") said the seven failing tests in `test_md_helpers` and `test_openmm_system_server` were an artefact of this image (`plumed.read_protocol` raising `Unsupported object type`). That was wrong, and this overturns it. The PLUMED commit `dc974d3` (2026-09-06) added `plumed.read_protocol` and `steering._restart_protocol`, both of which `XmlSerializer.deserialize` every XML restart file to look for a marker parameter, and `run_production` calls both on every run. The 24 tests that hand `run_production` a `<placeholder/>` restart file therefore died in that parse before reaching what they test; the same code also parsed a full State (19 MB for the 011 membrane) on every ordinary production restart for nothing. Both readers now check the marker name in the file text first and deserialize only when it is present or the caller needs the state (`need_state=True` for the PLUMED and steering runs themselves); an XML that is not a State is no protocol, and a marked XML that cannot be read is the respective restart-mismatch error. Regression tests in `tests/test_plumed.py` and `tests/test_distance_steering.py`. The seven tests pass; the full non-slow suite is green in this image.

## 2026-09-10 — Restart guard relaxed for eq -> prod, NaN retry at a halved timestep, and the membrane build's 700 CCD requests

Three fixes from the campaign's cli_skill_sif failures (evening). (1) `run_production` refused a 4 fs production from an equilibration that had run at 2 fs (`Restart integrator signature mismatch: timestep_fs: restart=2.0, current=4.0`, 011_membrane_6kuy r2, after the agent had correctly recovered a NaN at 2 fs). For an XML restart whose source is an `eq` or `min` node, timestep, temperature and friction differences are now warnings (`restart_integrator_changes`), because the loader transfers positions, velocities and box and the new Langevin integrator re-thermalizes within picoseconds; a different integrator kind, a prod -> prod continuation and any `.chk` restart stay hard (`_integrator_restart_verdict` in `mdclaw/simulation/restart.py`). (2) `run_equilibration` runs the low-temperature warmup at 2 fs or less and, on a NaN in the warmup or the NVT heating, retries that stage from its starting state with the timestep halved (`mdclaw/simulation/nan_retry.py`; warmup floor 0.5 fs, heating floor 1 fs); after a heating retry the rest of the equilibration runs at the timestep that worked, step counts are scaled to the same simulated time, and the result records `timestep_fs_requested`, the effective `timestep_fs`, a warning and the attempts. The eq_001 NaN on 011 (max force 2700 kJ/mol/nm after 5000 and after 50000 minimizer iterations, blow-up at 4 fs in the 50 K warmup) would have completed inside the node. (3) 014_membrane_6zdv's `build_amber_system` took about 450 s (395 s in `system_generator_init`) against 22 s on 004; r1 and r2 ran out of their 20 minutes in it. Reproduced offline on r3's solv node under cProfile: 44 s of a 124 s build were `openff.pablo.topology_from_pdb`, of which 27.5 s were 685 calls of `_download_cif`: Pablo asks the CCD for a residue name each time a residue with that name fails to match, and the lipid21 fragments `PA` / `OL` are not in the CCD, so every lipid residue produced one HTTP 404 before Pablo gave up and the `PDBFile` fallback ran. One request per lipid, some 700 per membrane build, is why the stage scaled with lipid count and with how many of the six agents were hammering files.rcsb.org at the time. `load_topology` now looks each unknown residue name up once and registers a miss as an empty definition list, so Pablo answers "no definitions" at once; the same build makes 2 requests instead of 685, `load_topology` drops from 44 s to 5.7 s and the build from 124 s to 93 s in isolation. Finer `_stage` markers (`ligand_internal_bond_patch`, `template_internal_bond_patch`, `external_bond_search`, `orphan_glycam_cleanup`) stay in the node metadata. Tests: `tests/test_nan_retry.py` (9), `tests/test_topology_pablo.py` (+2); `test_md_helpers` and the loader's users pass except the seven pre-existing plumed failures of this image. Skill pages `run-eq.md` and `restart.md` describe the retry and the relaxed check. Not committed; needs the SIF rebuild before any campaign sees it.

## 2026-09-10 — Shared floyd SIF and GHCR `latest` rebuilt from committed source 5674ca7

`/data/mdclaw.sif` → `/data/mdclaw/mdclaw-amd64-5674ca7a89a5.sif` (sha256 `e34756f7…`, 5,973,942,272 bytes), built from a clean checkout of `5674ca7` and published as `ghcr.io/matsunagalab/mdclaw:{5674ca7a89a5,latest}` (digest `sha256:c86c67ca…`); the `0.6.8` tag still points at the 2026-08-30 build. Replaces build3 (kept for rollback). Contents new since the last full build (1689f13): ligand-chemistry through membrane prep, mdahole2/HOLE, in-image sbatch environment hand-off plus today's worker-PATH fix, and the committed check_job controller lookup. Evidence: image `test-container.sh` 24/24, SIF with `--nv` 26/26, live n2 jobs 137275 (sleep, COMPLETED 0:0), 137276 (exit 7, FAILED 7:0), 137277 (solvated peptide min→eq→1 ps CUDA production, COMPLETED, nvidia-smi bound) — all reported through `state_source=scontrol` with the Slurm directory alone as `MDCLAW_SLURM_PATH`, the setting that broke the b7426fa candidate. Workspaces under `~/tmp/mdclaw/container_build_20260910_{b7426fa,5674ca7}`; the b7426fa image was pushed to GHCR under its sha tag before its defect surfaced and is superseded, not deleted. Docker build 26 min, push 29 min at ~4 MB/s, SIF 45 min via `docker-daemon://`; `/` dropped to 20 GB free after the second build and needed `docker builder prune --keep-storage 30GB`.


## 2026-09-10 — CLI proposals C1-C8 and skill edits S1-S5 implemented; one regression found on the way

Implemented the whole list from the investigation note (`docs/research/cli-agentic-usability-20260910.md`), in the recommended order, against the running campaign's evidence. C1: `create_node` attaches an omitted parent to the single *open* frontier node of the parent type (pending and running allowed for chains, failed never, ambiguity refused) and otherwise answers `parent_required` with `candidate_parents` and `candidate_commands`; `source_already_exists` names the existing node. C2/C3/C7 (`mdclaw/_envelope.py`, `_cli.py`): every result starts with `success, code, message, node_id, node_status, next_action, next, warnings_count, result_file, dag`; `--output brief` (default) stubs top-level values above 4000 chars and node tools write the full result to `nodes/<id>/result.json`; `--output id`; stderr is WARNING-only with an INFO tail kept for failure artifacts, `--log-file`, a `still running after Ns` heartbeat every 30 s, and a marker line before the JSON when stderr had output; a closed stdout (`| head`, two campaign crashes) no longer raises. `dag` (frontier and statuses) and `next` (run / create / wait / branch with the exact command; a blocked child gets its parent's step) ride on every result and error that knows a job dir. C4: `node_missing` lists the existing ids and the create command everywhere it is produced; `node_type_mismatch`, `node_terminal`, `node_terminal_transition_reserved`, `node_execution_context_invalid` and `invalid_node_type` carry the fix (`invalid_node_type` suggests the stage a made-up name refers to — the campaign used `split`, `membrane`, `fetch`, `build` — and long names such as `minimization` are accepted); guardrail actions rewritten for those codes. C5: `--json-input` unknown keys are `unknown_parameter` (21 `unhandled_exception`s in the campaign, mostly `job_dir` on helpers), node flags on a helper are `node_context_not_applicable`, enumerated parameters use `create_choice_error` (`invalid_parameter_value`; `clean_protein --protonation-method pdbfixer` was a raw `ValueError`), and `prepare_complex` registers the merged file it actually wrote (a re-run on a populated node writes `merge_2/`; four campaign crashes). C6: `mdclaw --workflow` prints the stage order, the stage tools per type from the registry, the three per-stage commands, the rules and the output contract; `--list` groups stage tools by stage first; `--help` is 40 lines instead of 191. C8: `bootstrap_md_workflow` creates `source_001` and returns `next` with the fetch command; `--pdb-id` runs the fetch in the same call, and a later `create_node --node-type source` hands the pending node back (`reused_existing_node`) so the skill text already installed from GitHub keeps working. A new preflight, `parent_not_completed`, refuses to run a child whose parent is not completed *before* the tool starts: stage tools resolve their own inputs and sealed the node as failed, so an agent that ran a chain member too early spent the node (observed in the smoke test the moment C1 allowed chains). S1-S5: `run-loop.md` no longer promises that parents resolve themselves, cites `next.stage_tools` instead of `<suggested_tool>`, adds the foreground/duration rule and the stdout/stderr rule; `md-prepare` and `acquisition.md` use the bootstrap's source node and `--pdb-id`; `hpc-run` says prep stages run in the foreground; `tool-output.md` documents the envelope; `skill-conventions.md` gains the rule that structural facts belong to the CLI.

Regression found and fixed: commit `8aa6be6` (the ligand-chemistry fix) inserted `_coerce_ligand_chemistry` between `@node_tool(node_type="solv")` and `def embed_in_membrane`, so the decorator moved to the helper and `embed_in_membrane` stopped being a node tool at the CLI level: no `node_context_required`, no type/terminal preflight, no CLI failure record, no handoff. The running campaign's SIF (`293fb7d1…`) carries that regression; it explains the two `embed_in_membrane raised FileNotFoundError … node.json` crashes (a guessed `--node-id` that preflight would have refused as `node_missing`). A source-scanning test now asserts that every tool calling `validate_node_execution_context(..., "<type>")` declares that type.

Tests: `tests/test_envelope.py` (26 tests: envelope order, brief stubs, result file, `next` for every node state, blocked child, fix-carrying errors, aliases, CLI-level `unknown_parameter` / `node_context_not_applicable` / `node_type_mismatch` / `parent_not_completed` not spending the node, `--output id`, `--workflow`, one-screen help, choice error, decorator audit). Full non-slow suite in the SIF: 2058 passed, 2 skipped; the 7 remaining failures (`test_md_helpers` ×6, `test_openmm_system_server` ×1) fail identically on a clean HEAD checkout (`plumed.read_protocol` raises `Unsupported object type` in this image) and are unrelated. Two tests were updated to the intended contract changes (`invalid_node_type` wording, `clean_protein` returning `invalid_parameter_value` instead of raising) and two to the pending-source reuse. Guardrail golden regenerated (335 codes). Not committed; the shared SIF still runs the pre-change code, so the campaign's remaining attempts do not see any of this — a rebuild and a re-run of the membrane tasks in both CLI conditions is the validation step in the note's section 7.

## 2026-09-10 — A Slurm-only MDCLAW_SLURM_PATH left the worker without nvidia-smi

Found while validating the b7426fa image on n2. The in-image `sbatch` sanitisation from 2026-09-09 hands the worker `MDCLAW_SLURM_PATH` as its whole `PATH`. The validation passed `/usr/local/bin` alone (where this cluster keeps its Slurm clients and singularity-ce), so the worker had no `/usr/bin`; `singularity --nv` resolves the binaries in `nvliblist.conf` through `PATH` (`use nvidia-container-cli = no` here), so `nvidia-smi` was not bound and the GPU payload died with `nvidia-smi: command not found` (job 137269) while OpenMM CUDA itself would still have loaded through ldconfig. Same image, same script, same narrow path: fixed source overlaid → job 137273 COMPLETED with `/usr/bin/nvidia-smi` in the container; installed unfixed code → job 137274 FAILED 127. `run_command` now appends `/usr/local/bin:/usr/bin:/bin` after `MDCLAW_SLURM_PATH` (deduplicated, never ahead of the site's entries), which is a no-op for the documented `MDCLAW_SLURM_PATH="$PATH"` route in `skills/hpc-run/sif-slurm.md` and also keeps a source-built `singularity` reachable when a site names only its Slurm directory. Host-launcher users never enter this branch. The b7426fa image and GHCR `latest` carry the defect; rebuilding from this commit.

## 2026-09-10 — check_job asks the controller before accounting; unknown state is reported, not inferred

The lab cluster runs `AccountingStorageType=accounting_storage/none`, so `sacct` never has a record and the old `check_job` answered `success:false "No records found"` for every finished job (the limitation recorded on 2026-09-07). `scontrol show job` keeps a finished job for `MinJobAge` (300 s here), so `check_job` now queries squeue → scontrol → sacct, matching the controller record by job id (array tasks by `ArrayJobId_ArrayTaskId`), and every successful observation carries `state_source` and `checked_at`; non-terminal states no longer expose a placeholder exit code. When nothing can establish the current state the CLI returns `slurm_status_unavailable` with `state` unset and, if the tracker holds an earlier observation, a separately labelled `last_observation` — never a fresh success reconstructed from old files. The 2026-09-08 build3 SIF already shipped this change as an uncommitted repack (live jobs 137257/137258/137259 on n2: COMPLETED 0:0, FAILED 7:0, tiny MD COMPLETED, all via scontrol); committing it now so the next image builds from source. Review before commit moved the tracker/DAG finalizer out of the query `try` blocks (a tracker write error was being swallowed as "scheduler unavailable" and the finalizer re-run on the next lookup) and made the no-client message name `MDCLAW_SLURM_PATH`. Focused suite 370 passed, ruff clean. Rikyu impact: `scontrol` resolves through the same `MDCLAW_SLURM_PATH` lookup as `sacct`; a missing client degrades to the previous sacct path; the sbatch submission path is untouched.

## 2026-09-10 — What the CLI must say about its DAG, and what the skill must not: root causes from the campaign

Investigated at the user's request from the first 33 sealed `cli_skill_sif` and `cli_sif` attempts of the three-condition campaign (membrane tasks 001-011), then revised after a deeper pass: the no-skill condition's missing workflow model (23 of 30 timeouts, 11 help reads per attempt, agents reading the package source inside the image) is the smaller finding. Four root causes also cost the skill condition and are the CLI's to fix. (1) `create_node` resolves an omitted parent only to a completed node, so the canonical `min -> eq -> prod` pre-creation for a Slurm chain fails, and the failure is filed under `node_context_required` with a hint about running tools; 11 of the skill condition's 24 errors, plus a cascaded `slurm_node_unavailable`. (2) Results are hard to consume: a `prepare_complex` result was 2.9 MB (coverage and identity maps that are already artifacts); agents merge stderr into the JSON stream in 52 % / 91 % of invocations and their parsers failed 3 / 14 times, which made a skill agent believe a successful fetch had failed, re-run it, and receive `node_terminal`; truncated results led to duplicate `create_node` and a `source_already_exists` that names no id. (3) Validation gaps: `embed_in_membrane` lacks `@node_tool`, `--json-input` passes unknown keys to the function (`TypeError` for `job_dir`), `clean_protein` raises a raw `ValueError` for a bad enum, and `prepare_complex` on a populated node registers a fixed artifact path it did not write. (4) Long stage steps print nothing, so agents background them and poll (68 backgrounded, 338 sleeps in the skill condition) although the tools take 22-80 s. The note `docs/research/cli-agentic-usability-20260910.md` draws the line: the CLI owns the DAG contract, state, result envelope, validation, self-description and progress; the skill owns scientific procedure and site discipline and must not restate mechanics the CLI reports at run time. Proposals C1-C8 (parent resolution for chains, brief envelope with result files and stdout hygiene, a `dag`/`next` block on every workflow result, fix-carrying errors, validation completeness, `--workflow` and short help, heartbeats, bootstrap creating the source) and S1-S5 (skill pages cite `next` instead of restating rules). Expected to remove about 20 of the skill condition's 24 errors. No code changed yet.


## 2026-09-10 — Shared SIF rebuilt with the ligand-chemistry and in-container sbatch fixes

Cluster-local runtime update, same procedure as the HOLE bundle: `apptainer build` from the current hole2 image with `%files` replacing the two changed source files and adding `/opt/mdclaw/ligandfix-source-manifest.json` (142 packaged files with SHA-256, source tree `f01d177dce29…`, commit `8aa6be6`). `%labels` does not overwrite existing labels, so the labels were patched in a sandbox and the SIF rebuilt from it; `org.mdclaw.runtime.bundle_id` is now `8790442a8951` and `org.mdclaw.source.commit` records the commit. The first acceptance job failed to start because login-node `/tmp` is invisible on compute nodes; the staged shared copy passed instead (job 92899: container smoke 28/28, OpenMM CUDA/PME, cuFFT prefault, PyTorch FFT). Activated by switching the shared compatibility path to `…-ligandfix-8790442a8951.sif` (sha256 `293fb7d1…`) with rollback link `…pre-ligandfix-20260910.sif` to the HOLE bundle. Not a tagged/GHCR release. Evidence under `.validation/ligandfix-20260910` and `/tmp/mdclaw-ligandfix-candidate`. MDDataBench specs under `runs/prep` now pin the new digest.

## 2026-09-10 — embed_in_membrane crashed on every prep that carried a ligand

Observed on the MDDataBench three-condition campaign (task 003_membrane_5zk8, cli_sif): `embed_in_membrane` raised `TypeError: argument should be a str or an os.PathLike object ... not 'list'` at the `ligand_chemistry` handoff. `prepare_complex` registers `artifacts["ligand_chemistry"]` as the list of ligand records itself and `solvate_structure` consumes that list directly, but the membrane path treated the resolved value as a file and called `Path(value).read_text()`. Every membrane system whose prep included a ligand therefore failed at embedding in both CLI conditions. Fixed by `_coerce_ligand_chemistry`, which accepts the records, a single record, or a JSON path; unit test added. The shipped SIF (0.6.8 HOLE bundle) still carries the defect until it is rebuilt; the campaign was stopped on 2026-09-10 pending that rebuild.

## 2026-09-09 — Direct-SIF Slurm route: what Rikyu needed beyond the skill page

Verified the "skills + SIF, no checkout" route (`skills/hpc-run/sif-slurm.md`) on Rikyu with the current image. Two gaps, both fixed. First, the page's bind list was insufficient: the image's synthetic passwd lacks `SlurmUser`, so every client died with `Invalid user for SlurmUser slurm` / `Unable to process configuration file`, and the host passwd lacks the NIS account (`Invalid user: rku00161`); binding the whole plugin directory (not only `libslurmfull.so`) is also required for `auth/munge`. The page now documents augmented passwd/group binds and the plugin directory. Second, `sbatch` called from inside the image exports the image environment to the job: job 90860 failed with `singularity: command not found` and logged `ld.so` errors for the `LD_PRELOAD` fusefix on every host process; 90861 with the runtime dir on PATH completed. `run_command` in `mdclaw/slurm/_base.py` now blanks `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONHOME` and all `APPTAINER*`/`SINGULARITY*` variables and uses `MDCLAW_SLURM_PATH` as the job's `PATH` for in-container `sbatch`; test added (`test_container_sbatch_hands_the_worker_the_host_environment`). The shipped SIF (0.6.8) predates this fix; MDDataBench's evaluator shim applies the same sanitisation for campaigns until the image is rebuilt. Local changes only, not committed or pushed; pi installs the skill from GitHub main, so the page fix reaches agents only after a push.

## 2026-09-09 — Remove superseded SIF images

Deleted three obsolete SIF copies to free quota. In home: `~/mdclaw/…-fusefix-54798ff98538.sif` (2026-08-01 hackathon-era build; the truncated copy noted in RIKYU.md) and `~/…-fusefix-ef0544563bb5-dirty.sif` (2026-09-07 pre-SMO-fix local build). In the group share: `/data1/rkp00079/…-6f171d2f0fa5.pre-sulfur-fix-20260908.sif`, the two-generation-old rollback recorded as `backup` in the sulfur deployment.json and in the SMO fix validation report. That rollback target no longer exists; the SMO-fix image can only be rolled back to the HOLE bundle's `pre-hole-20260908.sif` link.

Kept: current `…-hole2-b7526a99807f.sif` (target of the shared `…-6f171d2f0fa5.sif` path) and `…-fusefix-sulfur-561049fe8254.sif` (target of `…pre-hole-20260908.sif`). Both links verified to resolve after the deletion. `/data1/rkp00048` untouched.

## 2026-09-08 — Bundle working mdahole2/HOLE pore analysis

Added HOLE 2.3.1 to the shared conda environment definition and mdahole2 0.5 series to Python dependencies. Rebuilt the cluster ARM64 SIF with the official conda-forge HOLE binary and PyPI mdahole2 0.5.0, preserving MDAnalysis 2.10.0 and all other existing scientific dependencies. The conda interface package's Python metadata unexpectedly reported 0.0.0; used the correctly versioned official wheel instead, without modifying version strings.

Added an actual two-frame HOLE test and sph_process/sos_triangle surface generation to the container smoke. Expected radii 3.15/1.15 Å yielded 3.14947/1.14955 Å; VMD surface 173,709 bytes. Final SIF standalone execution passed. Slurm job 87590 on **rkp00079** passed all 28 container checks and the OpenMM/CUDA/PME/cuFFT/PyTorch GPU smoke. Shell syntax, repository-configured Ruff and diff checks passed. No user trajectory was modified or interpreted as a validated conducting pore.

HOLE bundle `b7526a99807f`, SIF SHA-256 `6c384c0ac9fb79e30e9314717e4037a2f61152498942edf6a0fd69f7ab9bbf21`. Switched the existing shared compatibility path to the new image after acceptance, preserving the immediately previous SMO-fix image and a `pre-hole-20260908.sif` rollback link. This is a cluster-local runtime rebuild, not a tagged/GHCR release; a fresh full Docker build/amd64 execution was not performed. See [implementation, tests and deployment](research/hole-20260908-runtime-validation.md).


## 2026-09-08 — SMO chemistry fixes implemented, tested and shared SIF switched

Implemented the [approved plan](developer/smo-root-cause-fix-plan.md): shared exact disulfide resolution, nearest-prep chemistry handoff into membrane charge calculation, Topology/System pair and input-conservation checks, common variant sequence classification, and skill diagnostics. Non-SMO GLYCAM testing exposed cpptraj renumbering; unique heavy-atom identity mapping now preserves the disulfide plan through it.

Normal CLI/DAG SMO acceptance on **rkp00079** (87152, observation branch 87258) completed minimization, 0.1 ns NVT and 2 ns NPT, plus the normal 2 ps warmup. All 476 residues, 3,738 protein heavy atoms, 9 exact S–S pairs and 475 peptide bonds survived; 105 saved frames passed independent geometry/finite checks. Automatic ions are Na82/Cl84, final charge −4.82e−14 e. Total 157,610 atoms is the expected −6 difference from replacing two more OPC waters with ions. NPT last 10%: 300.200 K and 1.028664 g/mL.

Regression: 639 broad tests passed; non-SMO pipelines 25 passed followed by 3 passing GLYCAM retests; 111 boundary tests passed, and the final 3 lipid tests passed (87560). BPTI old/new System XML is byte-identical and Reference energy/force differences at a common state are zero. Candidate standalone SMO checks passed, container smoke 27/27, shared-image GPU smoke passed (87562), lint and three skill validators passed. Counts overlap and are not a unique total. POPG fixture failures were traced to Packmol output assumptions, including automatic K+ neutralization, and corrected in the test.

At user request, the shared `6f171d2f0fa5.sif` compatibility path was atomically switched to `...sulfur-561049fe8254.sif`; the original is preserved as `...6f171d2f0fa5.pre-sulfur-fix-20260908.sif`. Active SHA-256: `c4073304b856f66fc322f1a0e5c497441fab5d645c6760df04b18a5d9583cd8a`. This is a manifest-identified cluster-local hotfix, not a new tagged/GHCR release. The user's old wrapper sets checkout PYTHONPATH and will override SIF code, so their checkout/skills must also be updated or the SIF CLI invoked without that overlay. Their repository and simulation artifacts remain untouched.

This does **not** overturn the investigation's finding that physical residue deletion was unconfirmed. The proven inspection inconsistency is fixed; all three original SMO topology files now report and split 476 residues consistently. Full evidence, limits, failed-fixture explanations and deployment/rollback paths: [validation report](research/smo-20260908-fix-validation.md).


## 2026-09-08 — Make non-SMO regression coverage a mandatory fix-plan gate

User required avoiding SMO-specific overfitting. Expanded the
[fix plan](developer/smo-root-cause-fix-plan.md) with explicit prohibitions on
SMO IDs/counts/charges and distance tuning in implementation, independent
expected-value checks, and a required non-SMO matrix. Coverage includes BPTI,
reduced cysteines, metal-coordinating sulfur, Amber variants/termini/caps,
2LOP membrane pipeline, supported lipid mixtures/charges/representations,
ff19SB-OPC and ff14SB-TIP3P, nonprotein components, neutralization intent,
ID collisions/insertion codes, and HMR/constraints. Identity-transform tests
must preserve chemistry without confusing legitimate protonation changes.

Freeze fixtures/baselines before implementation, run small contract/System tests
and existing pipelines, then require short non-SMO min/eq before the full SMO
2.1 ns test. Mandatory skips do not count as passes. The distributed SIF must
also pass representative non-SMO smoke tests. SMO-only success is explicitly
insufficient for release. Planning only; no product changes or tests launched.

## 2026-09-08 — Require the same SMO system through 2.1 ns equilibration in the fix plan

User required actual same-system execution tests in the implementation plan.
Expanded [the fix plan](developer/smo-root-cause-fix-plan.md) with fixed prep_008
coordinates/disulfide/identity artifacts, checksummed old/new controls, normal
embed_in_membrane -> build_amber_system -> minimization, then 0.1 ns NVT and
2.0 ns NPT at 300 K/1 bar, 2 fs, hmr=False as in the reported successful run.
The automatic ion correction must succeed without the user's patched solv_006
or direct OpenMM builder. Required results include 476 residues, nine exact SS
partners, +2e pre-ion and neutral final charge, peptide continuity, lipid bonds,
and trajectory/thermodynamic checks. Original nodes stay read-only in an
isolated fixture-based test study. The earlier optional-equilibration wording
is superseded: all three control/build/equilibration stages must pass before
claiming same-system verification. Planning only; no new computation launched.

## 2026-09-08 — Plan root-cause fixes for the confirmed SMO defects

Prepared an implementation plan, without changing product code or skills:
[SMO root-cause fix plan](developer/smo-root-cause-fix-plan.md). The plan unifies
resolved disulfide plans and execution, propagates prep chemistry into membrane
charge construction, validates exact SG-SG partners and assigned chemistry,
and shares residue classification/sequence generation between public and
internal inspection. Template-aware charge validation must account for terminal
states; it must not hard-code every CYX residue to zero charge. Diagnostic skill
instructions will consume the implemented structured results in one common leaf.

Four review units and acceptance tests are defined, including small negative
fixtures, DAG handoff, actual CLI consistency, SIF-overlay smoke checks, and a
final isolated SMO solv/topo/min validation. No Pablo replacement, automatic
repair of existing DAGs, or long production reruns are planned. Missing report
history remains closed as insufficient evidence, not an assumed design input.

## 2026-09-08 — Final SMO sweep identifies the old disulfide failure and closes missing history

Expanded the search to both rkp00079/rku00140 and the older rkp00048/rku00140
area (11,193 indexed files excluding git and membrane caches), all 6XBL failure
results/topology metadata/events, related old SMO trials, and skill/code history.
No original residue-loss measurement script or conversation transcript surfaced.
The owner-only home remains unreadable; missing-history attribution is now
closed as insufficient evidence per user instruction, not repeatedly deferred.

This corrects the preceding investigation's unresolved-old-Bug-3 assessment:
9/3 topo_004 records nine skipped_cys_protonated pairs, yet build_system forwards
the original unfiltered list to add_disulfide_bonds. On the exact saved PDB,
PDBFile initially has zero SS bonds; the raw adder adds nine while HG remains.
Current-source/SIF build reproduces the identical residue-6 CYS external-S
mismatch with the nine declarations; the same PDB with an empty list succeeds
(157,532 atoms). The skip/execution contradiction is present before and after
8e703ed, which changes clean_protein, not this builder path. Valid CYX inputs
worked in saved topo_003 on 9/2 and in current controls, so the report's stronger
claim of no viable input is disproven.

Also found current topo_001 node status completed conflicts with its preserved
failed event and metadata (neutralization_charge_mismatch, System +4e). The
charge guard did operate; mutable/manual status is not historical evidence.
The final investigation distinguishes proven CLI defects, skill diagnostic gaps,
disproven claims, and the three unavailable provenance links. No product/skill
fixes or user-file mutations were made. All diagnostic builds are finished.

See [final sweep and closure](research/smo-20260908-bug-investigation.md) and
[structured controls](research/smo-20260908-audit.json).

## 2026-09-08 — Follow the SMO residue-loss claim into CLI metadata and skills

Follow-up inspection found a concrete duplicate-inspector defect: public
inspect_molecules counts Amber variants, but structure.split's internal
_inspect_molecules_impl (also used by prepare_complex) counts only AMINO_ACIDS
in sequence_length. Actual split_molecules CLI runs on topo_005/006 return
num_residues=476 with sequence_length=447/468; extracted PDBs retain all 476.
The direct OpenMM topo_007 normalizes variant names, so the same internal
counter returns 476. This extends the earlier hypothetical standard-only
selection explanation to a reproduced product CLI output; it does not prove
which counter the reporter used. Public inspect_molecules and PyMOL
polymer.protein both count all 476 on each saved topology.

The user's 55 skill Markdown files match current except three Slurm/preamble
files. No skill directs deletion of Amber variants. Gaps: build docs omit
PDBFile fallback/template patching; no anomaly-investigation recipe distinguishes
sequence length, selection, residue labels, and actual input/System integrity.
The earliest saved explicit loss allegation is topo_007's hand-written warning;
no ordinary tool-start/completion evidence backs its alleged minimal repro.
Home-directory conversation/shell history is inaccessible under OS permissions;
shared files lack the measurement script. Requested a shared conversation-log
path. Scientific code, skills, and user files remain unchanged.

Details and CLI summary outputs are appended to
[the investigation](research/smo-20260908-bug-investigation.md) and its JSON audit.

## 2026-09-08 — SMO user artifacts distinguish charge handoff defect from alleged residue loss

Inspected the user's SMO files under /data1/rkp00079/rku00140 read-only and
rebuilt two ion-free topology controls in /tmp using current source and the
user's SIF. Upstream main is fce3dbc; user checkout is 1689f13. All 141 Python
files in both the user checkout and installed SIF match upstream except
slurm/_base.py; the scientific implementation is current (MDClaw 0.6.8,
Pablo 0.2.2). This extends the earlier synthetic-only disulfide investigation
with the actual user artifacts, without asserting what an unavailable
September 8 minimal-repro script did.

Contrary to the reported deletion, ordinary topo_005 contains all 476 protein
residues, HID x8/CYX x18/ASH x1/GLH x2, all 9 System SG-SG bonds, no peptide
breaks, and protein charge +2. Standard-20-only selection yields 447 residues;
topo_006 similarly has all 476 but standard-only selection yields 468. An
analysis selection mistake fits the alleged loss counts, but its original
script was not available. Ordinary topology construction already uses
PDBFile fallback plus template lipid bonds (28,951 internal, 442 external).

Confirmed root cause of wrong membrane neutralization: the temporary build in
_compute_membrane_net_charge receives no explicit disulfide plan. On the same
ion-free solv_005 input, no plan yields 8 SS bonds and charge 0; passing the
prep_008 plan yields 9 bonds and charge +2, with 157,452 atoms in both. Without
the plan, CYX A:490/A:507 each carry -1e despite their restored names; their
~3.345 A initial separation defeats automatic bond detection. Both builds
pass validation because CYX's H-pattern check does not demand an SG-SG bond.
The existing calculation already sums force-field partial charges: replace
neither Pablo nor a supposed charge heuristic to fix this specific defect;
propagate the bond plan and validate CYX connectivity/charge instead.

See [the investigation](research/smo-20260908-bug-investigation.md) and
[machine-readable audit](research/smo-20260908-audit.json). Product code and
user runs were not modified. No new MD or full equilibration analysis ran.

## 2026-09-07 — Correct shared-SIF deployment: no external launcher, dynamic Slurm paths

This supersedes the launcher design in the preceding shared-SIF entry. The user
required SIF plus existing system Slurm, rejected `/data/bin/mdclaw`, then rejected
a long bind recipe in the skill and required flexible executable discovery.
The Slurm CLI now resolves clients with `shutil.which`: current PATH by default,
or an explicitly supplied MDCLAW_SLURM_PATH, without falling back to another
installation. The same absolute executable is used for checks and execution.
Only container-origin sbatch calls clear inherited Singularity/Apptainer bind
and mount environment variables; native host calls and unrelated environment
variables remain intact. Mounts themselves still require standard launch flags;
the CLI cannot discover files hidden by the container namespace.

Removed the uncommitted external launcher, site-config file and launcher-only
tests. Replaced the 70-line skill draft with a 26-line conditional reference,
removed the old hpc-run prohibition on SIF-side Slurm, and corrected its existing
`--extra-flags "--nv"` example (reproduced parser error) to `--extra-flags=--nv`.
README, configuration reference and Notion now describe direct SIF invocation.
No host wrapper/function, Slurm path constants, SSH bridge or Slurm installation
inside the image was added. No system-wide Singularity settings were changed.

Validation: 166 relevant tests passed (26 new runtime cases, 132 existing Slurm
tests, 8 deployment tests); the installed Docker package separately passed all
26 runtime cases. Tests cover arbitrary/space-containing locations, PATH order,
explicit-path failure without fallback, argument preservation, both container
runtimes, native Slurm and preservation of other environment variables. Ruff,
skill validation and diff checks passed. Independent review found no additional
core regression. This is not a full autonomous Claude/Pi MD campaign.

Published the CLI-only update as GHCR `amd64-1689f1307992-build2`, digest
`sha256:cd98368175cba64dc7a99be49dcfd248081a64310e518d8a23223d90b9a412d5`.
Scientific dependencies are unchanged from build1. The changed installed file
`mdclaw/slurm/_base.py` has SHA256
`3fcd1b0cca4e58b95dd5c364c66791ef8e1f5f8ca0b1ab21c04ffb67cc9c8594`.
Built the SIF locally (no GHCR re-download), then validated actual host resources
discovered from PATH, Slurm config, ldd and the MUNGE socket. No outside `env -u`
or source overlay was used: submit/check/cancel passed (137245), and n2 GPU job
137246 completed with ExitCode=0:0, the installed CLI hash verified and all four
OpenMM/PLUMED/TorchForce force/energy/integration checks passing.

`/data/mdclaw.sif` now points to
`/data/mdclaw/mdclaw-amd64-1689f1307992-build2.sif`, SHA256
`8d73bb8d060641383123aaac3e0386f51c3057ace1ea39d8032fc9913565aa97`.
The immutable manifest is beside the versioned SIF. Build1 and the original
local-image backup are preserved. The two files formerly in `/data/bin` were
moved to `/data/mdclaw/retired-launcher-20260907` (recoverable, not used).
Logs and generated scripts are in
../container_build_20260907_1689f13/runtime-cli-smoke; the controller completion
record was captured immediately after completion. Existing accounting/text-summary
limitations remain as described below. Source changes are not yet committed.

## 2026-09-07 — Clone-free shared SIF deployment and real Slurm/CUDA validation

Added a standalone site launcher (`scripts/mdclaw-shared`) and reviewed lab
Slurm/MUNGE binds. Installed as `/data/bin/mdclaw`; users need no checkout or
host Python. The launcher imports the SIF package with Python isolation and
removes inherited Singularity bind lists before Slurm exports its environment.
Real job 137238 exposed why that removal is necessary: floyd's library path
was otherwise propagated into n2. Setup now preserves an explicit existing SIF;
README/admin docs and the short common skill preamble describe image-mode use.

Built source 1689f13079920480eb87c08258cc63a004565c55 with one build-only repair:
the SWIG std_vector check requires `-c++` (reproduced before changing it).
Pushed `ghcr.io/matsunagalab/mdclaw:amd64-1689f1307992-build1`, OCI digest
`sha256:ef2e0ad0d26a24e2433ba5e6fd8c3eab4b364a379836ecea063ce666da3fc147`.
Per user request, generated the SIF locally instead of downloading it again.
Published `/data/mdclaw.sif` ->
`/data/mdclaw/mdclaw-amd64-1689f1307992-build1.sif` (read-only), SHA256
`2839e3daaa8ad4c02fec8fc25eab33ea06d8de9ef4942311db463cfe8c74a71c`.
The previous local image is preserved as `mdclaw.sif.20260907-pre-shared.bak`;
local `mdclaw.sif` now links to the shared image. Provenance manifest is beside
the versioned SIF; build/test logs are in ../container_build_20260907_1689f13.

Validation: Docker and SIF basic runtime checks each 23/23; installed-package
MDDB/report/PLUMED CPU regression 95 passed, 1 CUDA case deselected; launcher and
deployment regression 13 passed. Real SIF submit/check/cancel passed (137240).
Final GPU job 137242 on n2's GTX 1080 Ti passed OpenMM, PLUMED with/without PBC,
and TorchForce energy/force assertions and short integrations, using installed
MDClaw's TorchForce preload helper. Initial GPU probes also corrected a test's
half-box force ambiguity and missing MDClaw preload, not scientific source.
No full Claude/Pi autonomous MD campaign was executed for this deployment.

Limits: accounting is disabled, so archived check_job cannot prove completion;
the controller had already purged 137242 at publication recheck, while its
four successful GPU assertions remain in the saved output. Inspect-cluster's
existing text fallback misaggregates heterogeneous GPU types; use per-node
sinfo for that detail. These pre-existing limitations were documented, not
expanded into unrelated code fixes. Notion's lab guide now uses ~/work, two
bashrc variables, the shared launcher, and one MD-through-MDDB-export prompt.

## 2026-09-07 — Fresh 007 pi/DeepSeek run at committed source 9d769f0

User requested another 007 execution after the preparation fixes were committed.
The prior validation007_conditions result is already 20/20 PASS; preserve it.
Initialized exactly one new attempt in ../validation007_20260907_9d769f0/campaign,
using the same fda021e evaluator, task, SIF, pi/DeepSeek-v4-flash model, 5400-second
agent limit, 8-hour MD limit and n2/n4 allocation. No solver prompt coaching.
Source/skills come from a clean committed 9d769f0 clone, frozen by init_experiment;
tree SHA256 84aca23a86df3cbfc39beeadbc0d5f51a27bcd53f1b9c497c4520c6f97240d51.
Manifest requires the source overlay and cluster config selects overlay mode.
Runner PID 3621102 and 600-second file-monitor PID 3621104 were verified alive;
agent_start recorded at 2026-09-06T15:40:15Z (Sep 7 JST). No MD job submitted yet
at this initial check and no result claimed. Monitoring writes progress.json /
monitor.jsonl, not automatic chat messages; it stops when this attempt is scored.
All older runs and unrelated scheduler jobs were left untouched.

## 2026-09-07 — Review and validate the pending 007 preparation fixes

Reviewed the two previously uncommitted changes: automatic forwarding of prep
arguments into declared-condition validation (including range spelling comparison
without flattening join-group membership), and protonation/histidine override
routing by each retained component's build window. File locations remain outside
scientific condition comparisons; source identity and normalized execution modes
remain explicit. Missing-in-window residues are left for the existing rebuild/
protonation checks; targets outside all selected components are rejected.

No additional code correction was needed in this review. Current-source SIF
validation: **525 passed** (120 preparation/protonation/condition-hint tests,
255 node/range/chain/metal/disulfide-handoff tests, 145 CLI/contract/registry tests,
5 real preparation/cleaning server smoke tests). Ruff and diff checks passed.
Also read the preserved successful 007 probe JSONs; these are historical evidence,
not a new 007 MD run. Commit scope includes only the two fixes, their tests and
the corresponding memo records; unrelated scratch files are excluded.

## 2026-09-07 — Reproduce direct clean_protein disulfide inconsistencies

Investigated the team report without its original input, commands or SIF digest.
On current checkout source overlaid into the SIF, a synthetic two-CYS/HG input
reproduced two failures through real clean_protein/PDBFixer/pdb4amber calls:
flat declared pairs returned success with CYX still carrying HG; nested cys1/cys2
pairs triggered an internally caught exception and returned CYS with success.
Both new regression cases failed before changing production code.

Reuse the existing pair-shape resolver in the direct cleanup stage. Explicit
pairs now add the S-S topology bond (without duplicates), and CYX conversion
removes thiol HG atoms/bonds with Modeller. No new CLI flags or chemistry defaults.
Regression checks retained residues, CYX names, absent HG and an S-S bond in the
intermediate topology; explicitly disabled pairs retain CYS/HG. These are synthetic
cleanup tests, not a full physical-system/topology/minimization validation.

A separate actual PDBFixer reader/removal-boundary test retained all 29 synthetic
residues (8 HID, 18 CYX, 3 acid variants or their standard-state equivalents).
Thus the reported 29/26-residue dropping and downstream hard geometry error
remain unreproduced; no speculative fix was made to those paths.

History matters: 3a233da (Aug 27) already addressed non-mutated ASH restoration in
the HPacker wrapper, and b592611 (Aug 30) updated merged-PDB CYX/HG reconciliation.
Both are ancestors of HEAD; the latter does not fix direct clean_protein's stage.
Their existing tests passed, but this is not a replay of Zhang's original system
or the entire supplied report. No wishlist/new-feature work was attempted.

Validation: **199 passed** across the new reproduction/disulfide/cap/prepare suite
(110), protonation/variant/selected server smoke checks (59), and existing
CYX-HG/sidechain-packer/PDB2PQR-variant regressions (30). Ruff and diff checks passed.
Only clean_protein and the new reproduction tests were changed for this fix;
pre-existing preparation edits remain untouched. No commit/push or MD run.

## 2026-09-07 — Keep steering outcomes out of protocol comparisons

Corrects the incomplete completion-metric filtering in 6a1d727. Exclude
final_distances_nm, target_errors_nm and progress-dependent final_centers_nm
from recorded-setting comparisons; all remain unchanged in each target's history.
Target distance and force constant in the protocol signature are still compared.
The regression exercises DistanceSteering.summary() directly without starting MD:
measurement changes and partial progress are not condition differences, while
target/force-constant changes remain detectable and complete summaries survive.

Validation: the new regression reproduced three failures before the fix; after
the fix, all **220 tests passed** across evidence, MDDB export, CLI, CLI contract
and registry in the SIF. Focused ruff and diff checks passed. No MD or upload.
Unrelated preparation fixes and their existing memo entries are left uncommitted.

## 2026-09-06 — Fix selected PDB connectivity and runtime bias comparison

Fixed both P2 review findings in the evidence tools. MDDB export now inserts TER
at gaps created by removing whole residues, using source topology indices so
PDB numbering gaps and insertion codes are not mistaken for cuts. Existing TER
and retained CONECT records survive. Reloaded PDB bond pairs must exactly match
the retained source topology; a mismatch aborts export before publishing a bundle.
The original three-GLY noncontiguous selection reproduced the invented peptide
bond before the fix. Regression tests also cover contiguous/partial selections,
crosslinks, existing breaks, insertion codes, and added/removed output bonds.

Report comparisons now include recorded positional/distance restraints, custom
force definitions and parameters, steering, and PLUMED protocols even when node
conditions are empty. Canonical per-Force XML hashes include nested parameters
while keeping report size bounded and preserving the existing attribute facts.
Protocol completion metrics and the steering initial-file locator remain in
history without becoming condition differences. Tests cover metadata-only bias
changes, biased versus unbiased nodes, actual OpenMM serialized bond centers,
force constants and group weights, and equal XML with different formatting or
artifact locations.

Validation: **216 passed** across `test_mddb_export.py`, `test_evidence_server.py`,
`test_cli.py`, `test_cli_contract.py`, and `test_registry.py`, including 21 new
regression cases, using checkout source inside the existing SIF. Focused ruff,
diff whitespace checks, and CLI tool discovery passed. The fixtures use
synthetic trajectories and OpenMM 8.5.1 System serialization; no new MD,
GPU/TorchForce dynamics, upload, or MDDB ingestion was performed.

## 2026-09-06 — Reviewer/reporter skill and offline MDDB bundles

Added the short `md-report` skill with one conditional MDDB page and both agent
discovery mirrors. It consumes `generate_md_report` for explanation and Methods
with selected BibTeX, retains citation/fact gaps, and asks about ambiguous leaves
even in autonomous mode. It does not launch MD, pool replicas, or upload data.

Added `export_mddb`: target-scoped, offline YAML + paired solvent-stripped PDB/DCD,
with a manifest, report and bibliography. Replicas are separate `mds`; selected
PDB atom/bond identities must match within a project, otherwise separate projects
are required. Existing combined-trajectory artifacts can be exported; no implicit
continuation concatenation occurs. Original atom/residue labels and frame order
are retained; water and standard Na/K/Cl counter ions are removed by default,
while lipids, ligands and other ions/metals are kept. Streaming conversion checks
source stability, atom/frame counts, coordinates and periodic boxes; native DCD
logs are redirected to stderr to keep CLI JSON parseable. Output cannot overwrite
existing bundles or enter immutable node directories.

Pinned the official MDDB-workflow template/schema to commit
`4e6dceeee67ce83650eed4aa2cfffe10107e2564`; original template SHA256:
`7278c91e564daa3aee9498fa9dd348d29666069f40805b507d21347fe53cdc82`.
The packaged YAML is a minimal instance, not a replacement schema. PDB is explicitly
a structure fallback (`input_topology_filepath: 'no'`), not a full force-field
topology. Frame spacing uses ns, timestep fs, temperature K; recorded CSV/frame
times take precedence over nominal output frequency. Author/contact/license/method
confirmation is an exporter safeguard, not a statement of mandatory web fields.

Validation: **195 passed** across `test_mddb_export.py`, `test_evidence_server.py`,
`test_cli.py`, `test_cli_contract.py`, and `test_registry.py` using checkout code
inside the SIF. New export tests cover synthetic multi-component PDB/DCD data,
stride/chunk combinations, replicas/separate projects, selection, runtime settings,
invalid/conflicting metadata, malformed sources, ancestor/combined-DCD targets,
immutable paths, and actual CLI JSON. All **12 generated YAML files**, including
per-MD overrides and relative file paths, passed the pinned upstream Pydantic
schema with strict unknown-field checking (only logging/constant imports stubbed).
Evidence/export lint and both affected skill validators passed. No MDDB ingestion,
server acceptance, upload, real-data deposition, or new MD simulation was performed.
The pre-existing limited deterministic citation coverage remains explicit; this
skill does not promote the larger research bibliography into verified selections.

## 2026-09-06 — Target-scoped multi-replica report CLI replaces job/study summaries

Replaced both legacy evidence-summary tools with `generate_md_report`; old CLI
names now return migration guidance instead of remaining executable aliases.
One job, a scoped study plan, or explicit `(job_dir,node_id,label)` targets use
the same node.json-based reader. Multiple leaves require user selection and
replicas/separate grouping, preserving failed/pending candidates. Reports retain
parent/dependency histories, shared ancestry, individual results, declarations,
recorded settings, XML runtime facts and source hashes. Common production prefixes
remain explicit; continuations or duplicate analyses of one production frontier
cannot masquerade as separate replicas. No pooling or independence/convergence
certification. JSON is read-only by default; optional new-directory export emits
JSON/BibTeX without overwriting outputs or immutable node directories.

The packaged 13-record verified subset supports explicit OpenMM 8, LF-middle,
related BAOAB, MC pressure control, HMR and selected protein/water parameters.
Other preparation/analysis/force-field/custom-method mappings and constraint
solver identity stay unresolved; this is not a complete automatic selector for
the 111-entry research inventory. Updated the existing analyze-skill call site,
developer reference, historical-plan notice and CLI contract; no new reporter
skill, MDDB exporter or LLM Methods generation is included in this CLI change.

Verified the real 007 prod_001 ancestry read-only: seven selected nodes through
prep_002, excluding failed prep branches, with serialized LF-middle and membrane
MC barostat settings. Focused evidence/CLI/contract/registry suite passed (163 tests), as did
lint and skill validation. The wider server-smoke invocation had 17 unrelated
Amber/MD failures at Pablo cache creation under a read-only container home
(188 tests passed); that broader scientific suite is not claimed green.
No campaign reruns, commits or pushes. Existing unrelated worktree edits preserved.

## 2026-09-06 — OpenMM citation evidence roles clarified

Checked official barostat, integrator/API, Constraints and bibliography pages.
Chow 1995 and Aqvist 2004 are explicit MC-barostat method references; Zhang 2019
is the LF-middle reference, with Leimkuhler 2016 cited as related BAOAB work.
Added the three absent records (111 keys / 110 unique DOIs total). No dedicated
membrane-barostat paper was identified in the checked section. Constraint solver
papers are implementation-derived supplements, not citations prescribed by the
Constraints section, and require per-run solver evidence. Updated the audit to
allow software/documentation provenance without a dedicated paper and distinguish
online 8.6 documentation from the inspected SIF's 8.5.1.dev-f7fa0c2 build.
Research documentation only; no runtime/skill changes or MD execution.

## 2026-09-06 — Lipid17 / FB18 / phosaa10 citation mappings resolved

Supersedes the second-pass unresolved status for these three parameter families.
Inspected the local SIF's openmmforcefields 0.16.0 XML and Amber parameter files,
not just web descriptions. Lipid17 v1.1 has six distribution-declared references;
retain them with their provenance role rather than inventing a standalone paper.
FB18 maps to Stoppelman 2021 (1c07547) plus the 2022 correction (2c06820).
The author README's 1c10971 DOI points to an unrelated DNA paper. Local
frcmod.phosfb18 is byte-identical to the author's fixed-commit corrected file;
all 55 proper-torsion groups / 235 Fourier terms in phosfb18.xml match after
unit conversion. phosaa10 requires both Homeyer 2006 charges and Steinbrecher
2012 phosphate-oxygen vdW parameters. Added nine verified bibliography entries
(108 unique keys, 107 unique DOIs total) and a detailed resolution note with
artifact hashes. Other model/ion/platform and arbitrary-input provenance questions
are separate, not retroactively closed. Research-only; no runtime code or skill
changes and no MD execution. Brace/uniqueness/whitespace checks passed.

## 2026-09-06 — Citation audit second pass: databases and algorithms

The initial 59-record audit missed OPM/PPM, MDAnalysis and free-energy methods.
Re-audited runtime imports/calls, the full force-field option catalog, membrane
orientation provenance, and external analysis registration. Added 40
publisher-metadata-verified DOI records: 99 unique BibTeX entries / 98 unique DOIs.
Added operation-to-citation tables and explicit unresolved mappings to
`docs/research/citation-audit-2026-09-06.md`; bibliography is its `.bib` companion.
OPM donor transfer is distinct from target PPM3 execution and is not a directly
measured experimental orientation. Built-ins use MDTraj; MDAnalysis is installed
for external analysis, while PyMBAR is used only for equilibration/timeseries in
the current runtime. Neither MBAR nor WHAM free-energy estimation is built in.
PyMBAR software credit must not be turned into a false estimator-use claim.
Also added Q, QCP/Kabsch, CCD, ETKDG/MMFF/UFF, legacy protein, DNA/RNA and GB
references. Verified MDAnalysis 2016 author order against conference manuscript
because its citation landing page disagrees. Lipid17/phosfb18 and several exact
constituent/runtime-version mappings remain open; this corrects any impression
that the first inventory covered every executable method. Unique keys/DOIs,
balanced braces and whitespace checks passed; no bibliography rendering tested.
Research docs only: no CLI, skill, simulation, commit or push changes.

## 2026-09-06 — Reviewer/reporter bibliography audit (research only)

Added `docs/research/citation-audit-2026-09-06.md` and its BibTeX companion.
Checked all 38 non-Zenodo DOI records from the retired citation inventory against
publisher-deposited Crossref records and added missing software/method references;
59 bibliography records total, including PMLR H-Packer and the explicitly labeled
AshGC working paper. Consulted Amber26 PDF sections/reference list and official
OpenMM, PACKMOL, OpenFF, PLUMED, PROPKA, PDB2PQR and MDTraj citation instructions.
Found GAFF's old DOI pointed to the 2005 erratum, multiple incorrect full author
names in OpenMM8/AmberTools, incorrect PDB2PQR/modXNA titles, and incomplete method
coverage. Corrected bibliographic records; documented NAGL versus AM1-BCC,
monovalent versus divalent/12-6-4 ion parameters, and remaining version-specific
mapping work. This is bibliographic verification, not full-text review of every
paper or a completed deterministic citation selector. No CLI/skill/runtime changes.
User accepts the obtained official MDDB-workflow YAML template as the initial
deposition format reference; no further deposit URL needed. Multiple terminal
nodes require asking the user about grouping/selection.

## 2026-09-06 — Prep condition checks consume actual arguments, not an allowlist

The next 007 agent declared and passed the same residue_ranges, but the prep
guard's hand-maintained argument list omitted that key. The previous direct
preparation probe did not exercise this DAG guard. Remove the duplicate
parameter lists: capture prepare_complex's input arguments on entry, preserve
resolved source identity and normalized modes, and exclude only DAG/file
locations. A signature-coverage regression ensures future scientific arguments
are forwarded too. Shared condition comparison accepts CSV/list range spellings
while preserving distinct join-group boundaries and rejecting mismatches.
No new CLI flags, schema, registry or skill prose.

The actual 007 input now completes through the CLI and a real source/prep DAG
with the original range/HIP conditions retained in the completed node. Evidence:
../validation007_conditions/probe_cli.py and probe_result.json. The previous
007 eq/prod/scorer jobs 137207/137208/137209 were cancelled at user request;
other tasks continue unchanged. Preserve both prior attempts. A fresh isolated
007-only pi attempt will use both fixes, not a modified in-flight source.

## 2026-09-06 — Protonation overrides routed to retained protein components

007's fresh pi run exposed a CLI bug: A:264 HIP was forwarded to both
A:-1-208 and A:219-305, so cleaning the first fragment failed with target not
found. Extend the existing chain filter with the component build window;
normalize legacy HIS overrides through the same route. Reject requests that
match no selected component before cleaning, rather than silently dropping
them. Within-window missing or wrong-residue targets retain clean_protein's
existing validation. No new CLI options or skill prose.

Validation: 165 related tests passed (routing, protonation, metal sites, range
selection and chain identity), 5 preparation/cleaning server smoke tests passed,
Ruff and diff checks passed. Repeating preparation on the actual 007 source
with its original ranges and A:264 HIP succeeds; the second fragment has HD1
and HE2, and the merged summary records B:264 HIP with original_chain A.
Evidence: ../validation_membrane_failures/probe_007{.py,_fixed.json}.

The user authorized stopping old 007 and rerunning with the fix. Old 007/009
agents and the campaign launcher/monitor were stopped; already-submitted 004
MD/scoring remains historical evidence. A separate frozen-source campaign
is prepared at ../validation_membrane_failures_routing_fix for all five tasks.
No benchmark PASS claim yet. The altloc bug was in the agent's ad-hoc recovery
script and was self-corrected; no evidence warrants lengthening the skill.

## 2026-09-06 — History-free PLUMED production, conda and container builds

Added `run_production --plumed-file` as an exclusive third bias route on the
existing prod DAG. PLUMED owns its per-step MOVINGRESTRAINT schedule; MDClaw
owns original/runtime inputs, protocol hashes, node-local logs/COLVAR, normalized
CV CSV and XML clock restoration. Partial ramps cannot become fixed umbrellas;
completed ramps inherit unchanged through fixed continuations. No new node
type, second schedule engine, arbitrary input-file resolver or history store.
The documented first subset is scalar distance/angle/torsion, COM/CENTER/GROUP,
WHOLEMOLECULES, RESTRAINT/MOVINGRESTRAINT and PRINT in nm/ps/kJ/mol. Unsupported
I/O, metadynamics/history and work-accumulation claims are explicitly excluded.

Both Dockerfiles and conda use the same pinned PLUMED 2.9.4/openmm-plumed 2.1
source-build helper, against the installed OpenMM prefix. Prebuilt conda plugin
constraints would downgrade OpenMM below the modern topology floor; they are
not used. Actual x86_64 validation built on the full 0.6.8 runtime and generated
a new SIF, without replacing the existing image. This is not a full rebuild of
all canonical Dockerfile layers; arm64 was updated but not built/run here.

A new periodic-image regression exposed upstream plugin 2.1's dangling
`setBox` pointer: its block-local array dies before calculation. The optimized
conda build failed COM/angle/torsion image invariance; moving that array to the
enclosing scope (two changed lines) made all three pass. The helper applies and
records `box-pointer-lifetime-v1`. Initial container tests happened to pass
without this patch; that compiler-dependent success was not treated as proof
that upstream was safe. Conda CPU production/DAG/geometry tests passed after
the patch; conda CUDA cannot run with this host's CUDA-12.4-capable driver and
the selected OpenMM 8.5.1 CUDA-12.9 build (unsupported PTX). Container CUDA works.

Validation includes real forces/finite-difference gradients, physical-element
COM masses despite HMR, periodic image invariance, interrupted XML restart,
two fixed continuations, nonzero origin and independent State-time offsets,
CLI JSON purity, hash tampering, unsupported inputs and SLURM generated binds.
The final patched validation image (`mdclaw:plumed-validation-fixed`, image ID
`3e3720a4feb8476b3612e9fe23731cff8cc6515cbf232b41db46a0cf55f36430`) passed all
619 focused tests, including the extended 21-test CPU/CUDA PLUMED suite.
Final container checks passed 25/25, and the unchanged 1AKE
continuation pipeline passed 7/7 (one existing pytest fixture deprecation).
The broader production smoke selection passed 15/15 (five registry/CLI cases
and ten scientific server cases, including native/TorchForce steering).
Final Ruff, shell syntax and diff-whitespace checks passed. No commit, push or
image publication was performed. The initial SIF remains explicitly pre-patch
validation evidence, not the final deployment image.

Live pi + DeepSeek completed five 2 ps CUDA CLI nodes: eq_001 → steered_X
(prod_002) → umbrella_X (prod_003) → umbrella_X_extra (prod_005), independently
eq_001 → steered_Y (prod_001) → umbrella_Y (prod_004). Independent audit checked
all 20 CV rows, harmonic energies, protocol/XML hashes, fixed centers (2.7/2.8
rad), exact step chains 100→1100→2100→3100, analysis-chain exclusion of steering,
and byte-identical source eq. The live run preceded the native lifetime patch
and the final `start_time_ns` metadata correction; these are covered by the
subsequent targeted tests. Actual XML times were already restored correctly.
Last CSV rows can precede a non-grid-aligned endpoint: schedule completion is
checked using XML/metadata, not the last sampled center. No equilibration,
target attainment or PMF convergence is inferred from this functional smoke.
Local evidence: `../validation_plumed/agent_study/report.md`, `audit.py`,
`agent.jsonl`, isolated conda prefix, validation Docker image and SIF.

## 2026-09-06 — Custom TorchForce steering on the existing production DAG

Extended the existing steering flags to `custom_force_script`. Distance and
TorchForce now share one step-grid/restart controller; no new node type, CLI
schedule language, integrator or plugin registry. `energy(positions, ctx)`,
user parameters and the topo-based `ctx.reference` retain their meanings.
`ctx.steering` adds applied progress and frozen actual input positions/box;
scripts still define arbitrary CVs, nonlinear schedules and potentials.
OpenMM global parameters drive both force evaluation and CV reporting.

XML + `steering.json` + hash-checked `steering_initial.npz` preserve the clock
and input geometry. Partial ramps refuse fixed handoff. Completed custom
ramps continue the same script/parameters at progress 1, including subsequent
umbrella extensions (`sampling_role=fixed_bias`). Script/parameter/reference
changes are refused within this managed lineage; ordinary static scripts are
unchanged. Python globals, online training state and external model weights
are not checkpointed. The production skill now includes an angular example
and independent common-eq → steered_X → umbrella_X instructions.

Validation: 608 focused tests passed, including real PythonTorchForce CPU/CUDA
energy/force/CV parameter propagation, nonlinear progress, interrupted XML
restart, two fixed continuations, invalid endpoints and provenance rejection.
Four server smokes passed (static/custom steering and fixed/native steering);
the existing 1AKE continuation pipeline passed all 7 steps: 619 tests total.
Ruff and diff whitespace checks passed. The pipeline retains one existing
pytest class-fixture deprecation warning.

Live pi + `deepseek-cloudflare/deepseek-v4-flash` completed five 2 ps CUDA
nodes through the source-overlay CLI: eq_001 → steered_X (prod_002) →
umbrella_X (prod_004) → umbrella_X_cont (prod_005), and eq_001 → steered_Y
(prod_001) → umbrella_Y (prod_003). Independent artifact audit verified equal
initial geometry/box, hashes, exact applied progress/centers, bias energy
against the logged angular potential, and both analysis collectors excluding
steered ancestors. Source digests, runner, raw agent log and audit are retained
under `/home/yasu/tmp/mdclaw/validation_torch_steering/`.

Functional smoke only: the 2.55/2.85 rad steering centers completed, but actual
angles at steering end were 2.530014/2.733168 rad. This is not target-attainment,
equilibration, overlap or PMF convergence evidence. The existing Python closure
runtime-System serialization warning remains; portable continuation rebuilds
from the topo triple, recorded script/parameters, XML state and sidecars.

## 2026-09-06 — Make steering discoverable from the production skill entry

The production SKILL.md now explicitly routes distance steering and umbrella
window preparation to distance-restraints.md. Documentation-only change;
verified the linked page and checked diff whitespace, with no MD rerun.

## 2026-09-06 — Native distance steering as independent production nodes

Implemented `run_production --steering-time-ns` with a separate
`--steering-update-interval-ps` (default 1 ps). Each branch starts from the
common eq's measured COM distance, follows a right-endpoint staircase
approximation to a linear ramp, then holds the target. Existing fixed biases
are unchanged. Use `prod` labels `steered_X → umbrella_X`; no new node type,
PLUMED dependency, TorchForce extension or sequential-window seeding.

The XML state's exact step counter and matching immutable `steering.json`
recover progress, including interruption inside an update interval. An XML
Context parameter binds the state to the protocol digest; missing/mismatched
sidecars and changed schedules are refused. Unfinished steering cannot silently
become fixed umbrella. Applied centers and bias/CVs are logged; metadata reports
schedule completion separately from actual target errors. Runtime System
serialization retains the final centers. Umbrella continuation rebuilds from
the topo ancestor, so no duplicate restraint accumulates. Both analysis lineage
collectors stop at the steered/fixed boundary (explicit steering diagnostics
remain possible); umbrella burn-in still requires scientific judgment.

Validation: focused CLI/registry/guardrail/restraint/node/SLURM suite 596 passed;
native-distance server smoke 2 passed (fixed baseline and steering/resume/
umbrella plus independent sibling); existing 1AKE continuation pipeline 7
passed. Ruff and diff whitespace checks pass. Tests include deterministic
uninterrupted vs segmented and interrupted XML restarts, incomplete-handoff
refusal, protocol mismatch, analysis exclusion and submission-condition checks.

Live pi + `deepseek-cloudflare/deepseek-v4-flash` ran the updated CLI/skill on
CUDA in an isolated small-protein study: eq_001 → steered_X (prod_002) →
umbrella_X (prod_003), and eq_001 → steered_Y (prod_001) → umbrella_Y (prod_004).
All four 0.002 ns nodes completed. Independent artifact audit verifies shared
initial distance (0.72772484 nm), distinct target-center traces, exactly one
native bias per runtime System, fixed umbrella handoff and analysis exclusion.
Targets were 0.50/0.55 nm; actual end-of-steering distances 0.73128/0.72512 nm
(errors +0.23128/+0.17512 nm). This deliberately tiny test establishes CLI/DAG
functionality, **not target attainment, equilibration or PMF convergence**.
Artifacts, raw agent log, independent audit and source hashes are retained at
`/home/yasu/tmp/mdclaw/validation_distance_steering/`; `audit.json` is the concise
record. No real SLURM submission or PR modification was performed.

## 2026-09-05 — 090 declaration mismatch checked before submission

090's retained min/eq completion events are successful; production failed on
`simulation_time_ns` declared 1.0 versus actual 2.0. The runtime guard was
correct. Corrected the external HANDOVER.md and added the companion benchmark
memo correction; no historical artifacts, scores or declarations were changed.

Extracted the existing condition comparator for read-only reuse. Node-linked
submit_job and submit_array_job now check literal production CLI commands
before sbatch, using the actual CLI parser and signature defaults, including
JSON-input precedence. A mismatched command target is refused too. Checked
keys are time, temperature, output frequency, trajectory format, platform,
device and seed. Inherited/derived values remain deferred; parents need not
be completed at submission time. Shell expansions, compound scripts and
wrappers are explicitly marked skipped, not certified. The full runtime
guard remains unchanged in policy and still runs on the compute node.

Final focused SLURM/node/condition tests plus node-server smoke: 361 passed;
CLI/SLURM initial regression: 247 passed. Ruff passes. Tests verify no sbatch
or node mutation on mismatch, default-time mismatch, queued-parent acceptance,
array rejection and uncheckable-script reporting. No real SLURM submission,
090 MD rerun or rescoring was performed.
Read-only replay against 090's retained node declaration, with the archived
production arguments, returns exactly `condition_mismatch` (1.0 versus 2.0)
before any execution. The historical run directory was bound read-only.
Normal suite: 1836 passed, 105 deselected. That run was collected before the
last array/default-mismatch test additions; the final 361-test run covers both
and the final array submission changes.

## 2026-09-05 — 042 root-cause repair, plan phases 1–4

Added explicit `prepare_complex --ligand-components` declarations (selection,
residue_name, isomeric SMILES), reusing split/clean_ligand/merge rather than a
second preparation engine. Input chemical classification remains protein;
requested ligand representation is separate and never inferred from length.
Only complete source subchains are supported: partial selection, external
LINK/CONECT bonds, conflicting chemistry and changed heavy-atom placement fail
closed. Original-source resolution and the 088 override guard remain active.
Heavy-atom source/prepared/merged correspondence is persisted in existing
ligand chemistry and chain identity artifacts. Skill guidance documents this
route and its boundary; existing positional Python arguments remain compatible.

Real 4MN3 CLI/DAG preparation produces one receptor and one +1 LIG, retaining
all 50 source heavy atoms and coordinates. This validates preparation, not a
production topology or improved MD score. Focused tests, including real 4MN3
and 12CA and source/guardrail regressions: 38 passed. Normal suite: 1824 passed,
105 deselected; changed Python files pass Ruff. Companion MDDataBench changes
validate chemical correspondence independently of these provenance artifacts.
Historical trajectory rescoring and paired pi/DeepSeek trials (phases 5–6)
were not requested in this implementation and have not been run.

## 2026-09-05 — 088 paired agent validation completed

All six pi/DeepSeek attempts completed MD and scored 20/20: baseline 3/3 and
fixed 3/3. All preserve the correct 255-residue sequence, author IDs and PHE260;
all 18 compute-stage import records match their frozen source. Baseline r1/r2
hit the false missing-126 refusal then recovered; fixed attempts did not hit it.
Both r3 agents omitted residue_ranges, so neither r3 exercised range coverage.
Fixed r2 first used malformed `5-260` (no chain prefix), then corrected it.
The historical artificial LYS addition did not recur. Final success-rate
improvement is NOT demonstrated; deterministic old/new prep establishes the
range-guard repair, and these runs show compatible end-to-end behavior.

All six actual production systems explicitly bond THR125 C to LYS127 N. Prep
retains the deposited 2.2543 A outlier; minimization gives 1.3442--1.3449 A.
The reference has the corresponding THR121--LYS122 bond, distance 1.3149 A.
There is no missing residue between them. Final suite: 1815 passed, 104
deselected; focused identity/source/real-12CA tests: 23 passed. The small CIF
entity-ID remapping amendment made after freezing has separate regression/SIF
coverage; the frozen agent comparison does not test that later amendment.
Details and evaluator scripts: `/home/yasu/tmp/mdclaw/validation088/REPORT.md`.

## 2026-09-05 — Source-based residue identity replaces integer range coverage

088's first prepared file was correct but `residue_range_not_delivered` falsely
requested 256 sites and reported absent author 126. Author 5--260 actually has
255 polymer positions, ending at PHE260. The agent's later renumbering and LYS
addition followed this refusal; a stripped PDB override then bypassed source
resolution. This corrects the earlier MDDataBench memo's denial of a tool defect.

Split now retains selected polymer rows (author number/insertion code, sequence
position, name, observedness) from mmCIF or SEQRES alignment. Coverage compares
ordered canonical sequences per component, detecting additions, deletions and
same-count substitutions; coordinate-only inputs explicitly have unknown
sequence completeness. Source/prepared correspondence survives merging through
the existing component index map. Peptide geometry is checked against the source
or normal rebuilt geometry, not numbering gaps. 12CA's deposited C125--N127
distance is 2.254 A and must not be falsely attributed to preparation damage.

In DAG prep, explicit structure files no longer bypass source resolution.
Coordinate overrides must preserve source polymer IDs/sequence, and source
sequence metadata is reattached. A deliberately different construct needs a
new source node; cropping uses selection options. Selecting an NMR model now
keeps sequence metadata and the author/sequence scheme rather than only atoms.

Validation: the existing non-slow/non-integration suite passed 1811 tests (104
deselected); the later model-selection regression plus focused identity/source
tests passed 19 tests. Real 12CA prep through the SIF passed with 255 delivered
sites, no added/deleted sites and terminal author PHE260, including final merged
identity validation. The unchanged 088 task/reference boundary regression passed.
The initial full-suite collection hit a read-only cache directory; setting
XDG_CACHE_HOME under /tmp resolved it. Ruff and diff checks passed.

The controlled pi/DeepSeek campaign is running outside both repositories at
`/home/yasu/tmp/mdclaw/validation088`: three baseline and three fixed attempts,
max two concurrent, interleaved starts. Baseline is 55118c5; fixed is an isolated
uncommitted patch snapshot of that revision, frozen tree SHA-256
`d08fc30b545b0b2f5d2c8c54a38b35feb15c2715d7fc2630bbdaa1390812b126`.
Skills are byte-identical; task, SIF and harness are unchanged. Agent-level
improvement and final MD scores are not established yet. No commit/push was made.

## 2026-09-05 — Reject invalid GPU flags in container configuration and submission

The full98 handover attributed six `-nv` failures to container commands in
submitted payloads. The retained `.mdclaw_cluster.json` files instead contain
`extra_flags: "-nv"` for 031, 034, 053, 066, 079 and 091; MDClaw's wrapper
inserted that value into generated scripts. The payload-only guard from
`09352d0` did not cover this route.

Shared flag validation now rejects the bare `-nv` token (including quoted
tokens) and malformed shell quoting with `container_extra_flags_invalid`.
`configure_container` validates merged settings before saving; both submit
tools validate existing settings before generating scripts or submitting jobs.
The error supplies the argparse-safe correction `--extra-flags=--nv`. An
explicit environment still bypasses an unused container configuration.

SIF-overlay validation: 274 tests passed across SLURM, guardrail registry, CLI
and registry suites, plus two new CLI subprocess smoke cases passed. Regression
coverage verifies saved settings and node state remain unchanged on rejection,
legacy settings can be repaired, and ordinary and array jobs both reject them.
No campaign settings were rewritten and no SLURM jobs were submitted.

## 2026-09-01 — Named residue-range groups preserve requested components

The preparation skill now treats every effective range, including ranges made
by "leave it out", as a separate component unless the prompt explicitly joins
it. `prepare_complex` and `split_molecules` add repeatable
`--join-range-groups`: each comma-separated group is one component, unlisted
ranges stay separate, and `--join-range-pieces` remains the join-all shorthand.
Invalid flag combinations, absent ranges, duplicate membership, and
cross-chain groups return `invalid_join_range_groups`. Results report each
resolved group and its residue count before topology.

Grouped components also carry all of their source ranges through missing-
residue handling and source-to-merged-chain remapping. A one-call RCSB 6KUX
replay joined A:29-173 to A:183-227 while leaving A:365-443 separate: the
reported component sizes and OpenMM chain residue counts were `[190, 79]`, the
173C-183N peptide bond was present, and no 227-365 bond was present. Focused
SIF-overlay tests passed 38 cases; full ruff passed; the full non-slow suite
passed 1789 tests with 7 skipped and 96 deselected.
## 2026-09-01 — PR #15 merged: the skill installer targets $PWD, with a known ownership gap

`scripts/install-agent-skills.sh` used to hardcode the checkout's own mirrors as
the destination, so one checkout could only wire up itself. It now installs into
the directory it is run from, letting one checkout serve several projects. Links
stay relative when the destination is the checkout (those mirrors are committed
and must resolve in every clone) and are absolute elsewhere. `--copy` unchanged.
Squash-merged as `69bb055` rather than a merge commit because two of the four
branch commits reverted the other two.

Verified before merging, on a fake checkout with the real `skills/` and a
separate project:

```
in-checkout links   byte-identical to the committed .agents/.claude mirrors (20/20)
cross-project       10/10 absolute links resolve, SKILL.md readable through them
--copy              10 real directories, 0 symlinks
reinstall           idempotent
tests               tests/test_deployment_scripts.py 6 passed
merge               clean against main
```

**Known gap, accepted at merge time.** In a shared destination the installer
still `rm -rf`s a same-named foreign skill directory and prunes broken symlinks
it did not create — silently, exit 0, no warning. Reproduced: a project-level
`.claude/skills/common/SKILL.md` belonging to another skill source was replaced
by an MDClaw symlink and its content lost. Scope is narrow and worth stating
precisely: **settings are never touched** (`settings.json`, `CLAUDE.md`,
`agents/`, `plugins/` all survived a run with `$PWD = $HOME`); only
`.agents|.claude|.codex/skills/` entries are at risk, and of the ten skills only
`common` is a realistic collision. Broken links are removed regardless of name.

None of this was possible on main, where the destination was always inside the
checkout and every entry there was this script's own output. The branch's third
commit (`e28c6221`) added exactly the right guard — `is_ours()`: a link into this
checkout's `skills/`, or a copied directory carrying `.mdclaw-installed` — and
the fourth commit dropped it along with `--user`. **Bringing `is_ours()` back is
the follow-up**, together with a test for the cross-project path: both installer
tests run with `cwd` at the fake checkout root, so `INSTALL_ROOT == REPO_ROOT`
and the new absolute-link path has no coverage. `docs/agents/deployment.md` also
still says "Default mode creates relative symlinks", now only true inside the
checkout, and does not mention the project-level install at all.

Direction for the next rework of skill deployment: follow what
https://github.com/mattpocock/skills does.

## 2026-08-31 — SLURM submit rejects container commands inside payloads

`submit_job` and `submit_array_job` now refuse payloads that invoke
Singularity/Apptainer `exec`, `run`, or `shell`, or `docker run`, before checking
or calling `sbatch`. The structured failure uses
`container_command_in_script`, names the offending command, and directs the
caller to pass the payload alone because `configure_container` owns the image
and flags. A deliberate caller can opt out with `--allow-container-command`;
if that command contains the campaign's erroneous single-hyphen `-nv`, the
successful result explicitly warns that GPU passthrough is `--nv`.

The existing HPC skill already states the same host/container ownership rule,
so it was left unchanged. SIF-overlay verification passed the SLURM and
guardrail-registry tests (125 passed), full ruff, and the full non-slow suite
(1783 passed, 7 skipped, 96 deselected).

## 2026-08-30 — Image and SIF rebuilt; the bundled membrane patches are actually in them now

`ghcr.io/matsunagalab/mdclaw:{latest,0.6.8,e92bbe80da5b}`, digest
`sha256:99f8d9ce5db9cf92bfd791796b6ac7b843994b5705f3cb765641d8b0c6f63eb0`; local
`mdclaw.sif` replaced (previous kept as `mdclaw.sif.20260829.bak`).

The 2026-08-29 image shipped `/opt/mdclaw/share/membrane_patches` **empty**: the
build-time warm-up failed and `|| echo WARNING` swallowed it, so anyone running
the image without a checkout overlay paid a 30-40 minute cold build on every
membrane composition. The warm-up step is gone; the patches ride in the package
(`mdclaw/data/membrane_patches`, 7 OPC + 6 TIP3P) and a build-time assertion now
fails the image if fewer than twelve manifests or either water model is missing.
It printed `bundled membrane patches: 13 ['opc', 'tip3p']` on this build.

Verified on the finished artifacts: `test-container.sh` 24/24 with GPU on both the
image and the SIF; inside the SIF, with no checkout on `PYTHONPATH`, the baked
package resolves its own `data/membrane_patches` and both `DPPC+tip3p` and
`DPPC+opc` probe as cache hits. This image also carries every MDClaw fix from
`580d80d` through `d105ea0` (ion intent, range-piece components, insertion-code
solvation, glycan classification, thiol rebuild, piece-aware remap).
## 2026-08-30 — Rikyu 用 SIF を d105ea0ef8cb で焼き直し。SIF 内 pytest は HOME を与えないと 2 本落ちる

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-d105ea0ef8cb  (未 push)
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-d105ea0ef8cb.sif
        6,883,450,880 bytes
        SHA-256 b0b701ef4382395026566ca6f8941f190531ca82d138465721e7a6db193ae30b
smoke   Docker 24/24、SIF からも 24/24 (GPU は SKIP)
tests   SIF 内で prepare_complex / phosphorylation / disulfide 4 本 / solvation /
        membrane orientation / sidechain packer / chain identity / registry / cli /
        guardrails = 482 passed
```

`~/Downloads` にあった `94eb819acd5e` は HEAD の祖先で、間に `mdclaw/` が 46 ファイル
(membrane, water, pdb_identity, disulfide, phosphorylation, prepare_complex, split,
sidechain_packer, prod_chain, node/inputs) 変わっていたので焼き直した。ホストは Mac
(Apple Silicon / Docker Desktop 14 CPU / 7.7 GB、`BUILD_JOBS=6`)。実測はビルド ~13 分、
`docker save` ~1 分、SIF 変換 ~4 分、`limactl copy` ~2 分。変換経路は 2026-08-19 以降と
同じ (`docker save` した tar を Lima の `singularity-ce` に `docker-archive:` で読ませる)。

### SIF から checkout のテストを回すときは HOME を明示する

VM から `/Users/yasu` が ro で見えるので、SIF 内でそのリビジョンのテストを回せる:

```
singularity exec --no-home --bind <checkout>:/work --bind /var/tmp/fakehome:/fakehome \
  <sif> bash -c "cd /tmp && HOME=/fakehome PYTHONPATH=/work \
    python -m pytest /work/tests/... -q -p no:cacheprovider"
```

`HOME=/fakehome` を落とすと **2 本落ちる**。`--no-home` だと `$HOME` が存在しないか
read-only になり、

- `test_guardrails.py::test_build_amber_system_pins_exact_multistate_ion_templates`
  → `OSError: [Errno 30] Read-only file system: '/home/yasu.linux'`
- `test_membrane_orientation.py::test_packing_receives_an_already_oriented_structure`

**これはテスト呼び出し側の欠陥で、SIF やコードの欠陥ではない。** 書き込み可能な HOME を
与えれば同じ SIF で両方通る (2 passed)。`-p no:cacheprovider` も同様に必須で、rootdir が
ro なので pytest が `.pytest_cache` を掘れない。SIF 検証でこの 2 本が落ちたら、まず
HOME を疑うこと。

未実施: GHCR への push と、Rikyu 実機での `test-rikyu-gpu.sh` (SIF からでないと FUSE
経路を踏まないので実機必須)。

## 2026-08-30 — Range-piece chain remapping preserves residue identity

Source-to-merged-chain and source-to-topology-index maps now resolve a site by
its source author chain, residue number, and insertion code when one deposited
chain is delivered as several separate range pieces. Scalar mappings for
ordinary chains are unchanged. Disulfide, PTM, protonation-summary, glycan-link,
and missing-residue reporting consumers use the piece-aware resolution.

A SIF replay of 5YC8 chain A with ranges 16–214 and 380–458 under standard
protonation completed prep successfully: A:96–176 mapped to merged chain A,
A:413–416 mapped to merged chain B, two disulfides were retained, and CYS/CYX
reconciliation reported no unresolved endpoints. The full non-slow suite passed
1780 tests with 7 skipped and 96 deselected; full ruff passed.

## 2026-08-30 — CYX demotion now rebuilds the reduced-cysteine thiol

The task-016 defect below is fixed at the CYS/CYX reconciliation boundary.
When an explicit disulfide list demotes a pdb2pqr CYX to CYS and the residue has
no HG, reconciliation now sends that site through the existing CYS protonation
path (`present={HG}`). A two-Cys regression at 2.03 A confirms that an empty
pair list produces two CYS residues with HG while preserving the SG geometry.
A SIF replay on cached 1AY7 chains A+B completed prep successfully with no
disulfides and reported two rebuilt thiol hydrogens.

## 2026-08-30 — Defect report written by a benchmark agent: CYX→CYS demotion leaves thiols deprotonated

*Written verbatim by the pi + deepseek-v4-flash agent during MDDataBench task 016 (it found the
benchmark repo's CLAUDE.md from its workspace). Kept as a defect report; the workaround it found
is real and the underlying defect is open.*


Attempt 016 (1AY7, barstar complex, chain A 1–96 + chain B 1–89, TIP3P, 300 K
NPT, >=2.5 ns prod) hit a reproducible chemistry/topology fault worth writing
across attempts, in case the same task pattern recurs.

Root cause: the deposit's Cys7–Cys96 (chain A) SG atoms sit 2.035 Å apart (a
native disulfide). pdb2pqr (`--protonation-method standard`) names them CYX;
`prepare_complex --disulfide-pairs '[]'` correctly demotes CYX→CYS in
`merged.pdb`, but the demotion only renames — it does NOT rebuild the thiol
proton (`HG`). The demoted Cys7/96 therefore enter `build_amber_system` as
CYS with SG but no HG, still 2.035 Å apart. openff-pablo's `add_disulfide_crosslink`
(CCD patch) sees the deprotonated SG pair and forms an SG–SG bond;
`SystemGenerator.create_system` then dies with
`No template found for residue 95 (CYS) ... the atoms and bonds in the residue
match CCYX, but the set of externally bonded atoms is missing 1 S atom`.
`--disulfide-bonds '[]'` on the topo node did NOT help: the crosslink is formed
geometrically by pablo, independent of the declared bond list.

Fix (worked first try): branch prep and force the two named sites protonated
with `--protonation-states '{"A:7":"CYS","A:96":"CYS"}'`. The CYS spec in
`mdclaw/structure/protonation.py` has `modeller_variant=CYS` / `present={HG}`,
so the Modeller pass rebuilds the thiol H. With HG present on both SG, pablo's
crosslink condition (leaving atoms absent) is false, no SG–SG bond forms, and
the topology validation reports `observed_system_harmonic_sg_sg_bond_count: 0`.
Lesson: a "reduced-cysteine, no disulfide" request must protonate the CYS
explicitly via `--protonation-states`, or the build fails/misparametrizes once
the deposit has native SS geometry.

Submitted chain (all on `all`/n4, 8 h wall each, CUDA via `--nv` SIF):
min_001 136026 -> afterok -> eq_001 136027 (300 K, 1 bar, 1 ns NVT + 1 ns NPT)
-> afterok -> prod_001 136028 (3.0 ns, 300 K, NPT 1 bar, dcd every 10 ps).
Topology: ff14SB + TIP3P (ff19SB+TIP3P is blocked; TIP3P request therefore
forces the ff14SB+TIP3P pairing), HMR on (4 amu, 4 fs), PME 1.0 nm, HBonds.
Box 83.88 Å cubic, 61807 atoms, 52 Na+/39 Cl- (solute −13).

## 2026-08-29 — Phase (a) removes the seven pass2 ambiguities on the MDClaw side

- Disjoint residue ranges now produce separate components by default, with
  `join_range_pieces` as the explicit opt-in for joining them.
- Packmol receives a sequentially renumbered solute copy while the deposited
  residue identity is restored from the original; a residue-count mismatch now
  fails as `solute_identity_not_preserved`.
- The digit-first glycan guess is gone. Curated names/entity metadata classify
  deposited glycans, while installed GLYCAM templates cover topology-time names.
- The writable membrane-patch cache now defaults to the XDG user cache (or
  `~/.cache`) after the existing MDClaw environment overrides. Membrane skill
  recipes explicitly carry the chosen water model into embedding and topology.
- The HPC skill now submits the complete host-side `min -> eq -> prod` afterok
  chain immediately after topology. Equilibration guidance now states that the
  default k=100 restraints remain through NVT/NPT and that no unrestrained MD is
  run before the clean production checkpoint; the optional k=0 NPT stage is
  used only when explicitly requested.

RCSB-focused replays confirmed 010/012 produce 3/4 separate range components,
019/024 restore all 624/300 solute residues after Packmol-safe renumbering, and
043 prepares 9RQ as a non-glycan ligand with CCD-derived net charge -2. The
DPPC+TIP3P+0.15 M cache probe hit the bundled patch. Full agent/scored replays
were not run because their attempt workspaces and frozen harness are not in the
retained evidence; a no-tool Pi/DeepSeek dry run was attempted but did not
return in this managed environment.

Verification in `mdclaw.sif`: focused tests passed; the OpenMM NPT -> NVT ->
explicit-k=0-NPT -> production DAG passed 8 tests; the full non-slow,
non-integration unit suite passed 1778 tests with 103 deselected; ruff passed.
The combined `skills/**` edit is net -1 line. No MDDataBench files or scoring
thresholds were changed.

## 2026-08-29 — TIP3P membrane patches are bundled; the OPC-only cache never hit in a 28-attempt campaign

All seven bundled membrane patches were `opc + ff19SB + 0.15 M`, and every membrane
task in MDDataBench asks for TIP3P (the reference MDs used it), so the bundled cache
missed by construction: in `full100-pass2` (pi + kimi-k3, Rikyu) the 28 membrane
attempts each paid a cold packmol + equilibration build in `solv_001` — directly timed
foreground intervals of 1.5–2.3 ks — and the writable cache defaults to the CWD-relative
`.mdclaw_cache`, so the second replicate of a task could not reuse the first's patch.
Membrane agent wall time p90 sat at the 5400 s ceiling; three of the four agent timeouts
in that campaign were membrane tasks.

Six TIP3P patches (POPC, POPE, DPPC, POPC:CHL1 4:1, POPC:POPE:CHL1 2:1:1,
DPPC:DOPC:CHL1 1:1:1; `ff14SB` by `patch_equilibration_forcefield`, 0.15 M NaCl) were
built on floyd with `scripts/warmup_membrane_cache.py --water-model tip3p` (≈10 min each
in parallel) and added under `mdclaw/data/membrane_patches` beside the OPC set (7.6 → 14
MB). `probe_patch_cache` now reports `bundled` for all six TIP3P compositions and still for
all seven OPC ones. The warm-up script's default water model is now `tip3p`.

**DOPC + TIP3P did not build**: twice, deterministically, `Particle coordinate is NaN`
in the 50 K NVT warm-up right after the staged minimisation — a packing clash the
minimiser cannot clear, the same shape as the P18 packmol-memgen failure. The OPC DOPC
patch builds fine. Left uncached; no task in the current cast uses DOPC. Also still open:
the writable cache location (a per-user or per-campaign root would let rebuilt patches be
reused), and `embed_in_membrane` defaulting to OPC — two agents in that campaign omitted
`--water-model tip3p`, silently got an OPC membrane, noticed, and rebuilt.

## 2026-08-29 — Solvation ion intent and charge are explicit

- The canonical solvent-regime guide now maps absent/neutralised ion wording to
  the 0.15 M NaCl default, reserves `--saltcon 0` for counterions only, and maps
  `no ions` to `--no-salt`.
- `solvate_structure` reports `solute_net_charge_e` and requested-species
  `ion_counts` in CLI results and solv-node metadata. The charge is parsed from
  packmol-memgen after its prepared-residue estimate and MDClaw
  `charge_pdb_delta` corrections; this makes charge 0 plus zero counterions an
  explicit result instead of an inference from missing PDB ion records.
- `--no-salt` now passes `--nocounter` (and disables OpenMM neutralization), so
  it means no ions rather than counterions-only. SIF verification: ruff passed;
  solvation/guardrail/CLI tests passed (233), as did the membrane and
  phosphoprotein DAG pipelines on GPU (9).

## 2026-08-29 — DAG-derived analysis time axes

- Removed the fixed 100 ps assumption from RMSD, distance, and Q CSV output.
  `concat_trajectory` now pairs each trajectory with its energy artifact and
  prod `timestep_fs` in one continuation-chain walk, then writes
  `frame_times_ns.npy` from the retained energy `Step` rows after applying the
  same stride as the DCD. Mixed output cadences are represented directly.
- Fit and metric descendants resolve that artifact through the analyze DAG.
  Direct-mode and legacy inputs without it remain analyzable but emit
  frame-only CSVs; MDClaw no longer invents a `time_ns` value.

## 2026-08-27 — Distance-restraint selection and throughput correction

- This overturns the `CustomCVForce` implementation recorded immediately
  below: a 358,101-atom benchmark measured an 11.8% throughput loss from that
  wrapper, while direct `CustomCentroidBondForce` was indistinguishable from
  unbiased throughput. The production force now uses direct per-bond `k`/`r0`;
  report-time positions reproduce the same mass-weighted, minimum-image CV.
- Solvated-topology selections containing water or bare ions are rejected.
  Examples use topology-wide `resid` plus `protein`, because PDB `resSeq`
  numbers wrap and are reused by solvent.

## 2026-08-27 — Native harmonic production distance restraints

- Added the declarative `run_production(distance_restraints=...)` contract for
  harmonic atom/center-of-mass distances using native OpenMM
  `CustomCVForce` + `CustomCentroidBondForce`, avoiding per-step
  PythonTorchForce/autograd overhead.
- The exact biased coordinate and bias energy use the existing
  `collective_variables.csv` / `.meta.json` artifacts. Distance groups use
  explicit physical elemental mass weights so HMR does not change the CV.
- Biased `--continue-from` inherits the parent declaration and requires the
  portable XML state; binary checkpoint restart is rejected because the live
  System contains an added bias force.
- Scope is production-only harmonic distance bias. Equilibration restraints,
  flat-bottom/angle/dihedral potentials, PMF/MBAR, and changes to
  `analyze_distance` remain separate work.

## 2026-08-27 — Closed the custom-XML topology-builder contract drift

`build_openmm_system` remains the research escape hatch, but now reuses the
curated builder's final topology validation, system net-charge calculation,
topology-build stage breadcrumbs, result/metadata shapes, and structured node
failure path. Its `amber_metadata.json` records its own path before writing,
and package-relative openmmforcefields XMLs receive the same SHA-256 provenance
as curated bundles. The existing source-residue-name stamping behavior remains
unchanged; this work did not transplant Amber-specific variant restoration or
curated force-field guardrails.

The unconditional runtime import gate for `openmmforcefields` was removed as a
separate cleanup: arbitrary OpenMM-native or absolute-path XML builds do not
need that package. Package-relative XMLs supplied by openmmforcefields still
work when the optional package is installed and are hashed for provenance.

## 2026-08-26 — Reviewing the terminal route: two wrong measurements

Verification notes for the MODELLER terminal work, kept because both of the
review's own measurements were wrong before they were right.

All six MDDataBench tasks whose reference builds at a terminus now build it,
with the author numbering the deposit's own REMARK 465 gives:

    1CTF  N  A:47-52   ALA ALA GLU GLU LYS THR
    1EZ3  N  B:24-26   VAL ASP ARG
    1AIL  C  A:71-73   GLU GLU ASP
    1A62  C  A:126-130 ASN ALA ARG ASN LYS
    1E3U  C  B:265-266 GLY GLY
    6W9C  internal C:225-226 plus C-terminal C:315, in one complex pass

Two claims made during review and withdrawn:

The solvent-chain failure was blamed on this work. It predates it. Running
3RVW on `adfbbdc` gives `modeller_repair_reference_sequence_unavailable`,
"8 chain(s), 3 sequence(s)": the per-chain reference-sequence requirement was
applied to non-polymer chains, so the MODELLER route could not run on any
deposit carrying waters or ions. This patch repairs that as well as adding the
terminal route, which means the internal-gap path was reachable in tests and
not in practice.

Observed atoms were reported as moving up to 6.27 A far from any gap, on
3RVW chain A, which has no unresolved residues at all. That was a comparison
artefact: 3RVW carries A/B alternate conformers, the reader took the last one
seen, and MODELLER had been given A. Selecting A explicitly, the same residue
moves 0.0030 A and only four residues exceed 0.5 A -- D:133, D:134, D:139 and
D:140, every one of them beside a gap. MODELLER's own log settles it
independently: 48 of 5127 atoms selected, for both base optimisation and loop
refinement.

So a coordinate comparison against a deposit has to resolve three things
before it means anything: alternate conformers, symmetric side-chain naming
(Asp OD1/OD2 and friends read as 2 A of movement that is really 0.02), and
rigid motion. Correcting only the second, as the first pass here did, produces
confident numbers that are wrong.

The terminal case's own figure, with symmetry corrected: median 0.0377 A, and
outside the insertion_ext=2 anchors nothing exceeds 0.2 A. The anchors reach
4.4 A, which is the selection doing what it says.

## 2026-08-26 — MODELLER can build requested terminal residues, as predictions

Terminal missing residues now have a MODELLER route instead of falling through
the PDBFixer internal-gap guard. The policies are separate: PDBFixer accepts a
requested terminal segment through 5 residues, MODELLER through 10, and the
default remains to leave unresolved termini alone. The MODELLER ceiling is a
conservative policy for a one-anchor prediction, not an accuracy guarantee.

The repair stays in the whole-complex, gap-local design introduced in
`adfbbdc`: MODELLER selects each gap plus its two-residue anchor on the available
side, while partner chains remain fixed context. Output numbering is restored
from one shared exact target-site map, because previous-residue extrapolation
cannot number an N-terminal insertion.

Measured through the real PDBFixer-to-MODELLER route on 1CTF chain A, whose
deposit resolves 53-120 and requested range is 47-120:

- 74 residues returned, numbered A47-A120; all six requested N-terminal sites
  were present and the exact target-site validation passed.
- The A52 C to A53 N peptide junction was 1.361 A.
- The result is marked as a one-anchor deterministic prediction, not evidence
  that residues 47-52 are ordered experimentally.

The six benchmark terminal cases were then run against their cached deposits.
All returned the declared sites: 1E3U B265-266, 6W9C C225-226 plus C315,
1A62 A126-130, 1AIL A71-73, 1CTF A47-52, and 1EZ3 B24-26. Their terminal C-N
distances were 1.347, 1.350, 1.354, 1.350, 1.361, and 1.357 A respectively.
The four internal-only controls (1AHW, 3EOA, 3RVW, 3WD5) retained exactly their
declared internal build sites.

Caps remain independent in the public path. On 1CTF, building A47-52 and asking
for an N-terminal cap produced ACE46, ALA47, ALA48; asking for ACE without the
terminal-build switch left residues 47-52 unresolved and produced ACE52, GLU53,
PHE54. The latter carried no predicted-terminal marker.

1A62 exposed an ordering prerequisite: its three observed MSE residues reach
MODELLER as HETATM before the later PDBFixer non-standard-residue step. Passing
an `X` target made MODELLER reject the alignment. The repair now uses
PDBFixer's own MSE-to-MET decision in the target sequence and keeps HETATM
enabled for the template coordinates. If non-standard replacement is disabled,
the repair fails closed instead of silently changing the polymer chemistry.

Postconditions now check exact residue identities and order, retention of every
observed residue, finite coordinates, a 1.1-1.6 A terminal C-N junction, gross
heavy-atom overlap against the fixed template, and the existing declared
disulfide bounds. Segment provenance names N-terminal, C-terminal, or internal
location and records the exact sites built.

## 2026-08-26 — CV review: the definition holds, the sampling does not

Rebuilding the missing loops changed the premise behind one CV choice, so the
definitions were re-examined against the finished systems (apo prep_018, holo
prep_010) and reviewed by codex.

### The definitions are sound

`LB2_B` excludes B249-252. The original reason -- disordered in 9UT9, so the two
systems would not share the same atoms -- is gone now that the loop is built. The
exclusion still stands, on a different reason: those coordinates are *predicted*
in apo and observed in holo.

| CV | predicted residues, apo | predicted residues, holo |
|---|---|---|
| CV1 as defined | 0 | 0 |
| CV1 including 249-252 | **4** | 0 |
| CV2 (CRD loop) | 0 | 0 |

Including them would put a difference in modelling provenance inside the
observable meant to isolate a difference in ligand binding. Both CV groups hold
identical atom counts across the systems (LB2_A 1579, LB2_B 1525, loop 82 heavy).

### A worry that measurement dismissed

CV2 is a distance from a reference built out of the same lobes as CV1, so the two
could be geometrically entangled -- an apparent coupling that is really one CV
reading the other. Measured by moving the lobes and holding the loop fixed:

| CV1 change | symmetric opening | one-sided opening |
|---|---|---|
| +0.1 nm | 0.0000 nm | +0.0013 nm |
| +0.5 nm | 0.0000 nm | +0.0222 nm |

Exactly zero for symmetric motion, and under 5% of the CV1 change even when only
one lobe moves. The CRD loop sits almost perpendicular to the LB2_A-LB2_B axis
(axis-parallel component -0.05 nm of a 1.53 nm distance), which separates "depth"
from "opening" geometrically.

Two corrections from codex on this. `cv_compute.py` uses the mass centre of
`lb2_a + lb2_b` together, **not** the midpoint of their two COMs -- the two differ
by 0.316 A here. Redone with the reference the code actually uses, the numbers
above are if anything smaller. And switching CV2 to an axis projection, which was
considered, would be worse: the axis-parallel component is only ~0.5 A, so the
projection measures a different and much smaller motion than "insertion depth".

### The real problem is sampling, not the CVs

From the earlier partial apo umbrella set (39 windows, 3 ns discarded):

    rms_half_difference_kcal    8.96      criterion 1.0
    max_abs_half_difference     32.99
    neighbour overlap           min 0.086, median 0.19, none below 0.03

Window overlap is healthy and the halves still disagree by nine kcal/mol. That
combination rules out window placement and rules in unconverged sampling *within*
windows: umbrella sampling accelerates the CVs only, and orthogonal degrees of
freedom -- a rebuilt loop finding a different rotamer or backbone basin -- are not
biased and need not relax on the same timescale (Zhu & Hummer 2012). Adding
windows does not fix this.

Distances from the modelled regions to the CV groups, measured on the final prep,
say where that bites: apo's B249-252 is 4.93 A from LB2_A and A342-366 is 3.04 A,
while LOOP_B is 16.86 A from any modelled atom. So the exposure is on the CV1
side, and it is asymmetric between the systems (58 rebuilt residues vs 36).

### Open, not yet decided

- A seed sensitivity test: rebuild each system from a second MODELLER seed and
  re-run a few representative windows (inserted, barrier, withdrawn). If the
  spread is well below the apo-holo difference, record it as a limitation; if not,
  the whole PMF comparison is model-dependent and has to be reported that way.
- A matched-coordinate control: 9UTC with sucralose deleted, run as a third
  system. 9UT9 and 9UTC are separate reconstructions (3.18 and 3.33 A) with
  different disordered regions, so apo-vs-holo alone does not separate the ligand
  effect from the difference between two cryo-EM models.
- Window grid: the design values carry over, but the *trajectories* from the old
  structures must not. Rebuilt apo starts at CV1 3.628 nm against a current upper
  edge of 3.66 nm, so a short unbiased run should confirm the distribution does
  not press against the boundary before the grid is reused.

## 2026-08-26 — Declaring all disulfides explicitly, and what it exposed

The request was simple: declare every disulfide rather than relying on detection,
taking 9UTC's 17 as the reference because 9UT9 leaves A363/A366 unresolved. It
turned into the largest single change of the session, because checking all 17
found two that were wrong and a third problem underneath them.

### The three findings

| | |
|---|---|
| A363-A366 came out **11.65 A** | inside a rebuilt gap, invisible to MODELLER's template-derived restraints |
| A59-A102 came out **3.53 A** (4.47 A on 9UTC) | observed at 2.03 A in the deposit and moved anyway |
| observed heavy atoms moved a median **0.28 A**, max **15.37 A** | MODELLER re-optimises the whole model, not just the gaps |

The third explains the second and matters most. Comparative modelling does not
copy template coordinates; it rebuilds from template-derived restraints, and
those are weakest beside a gap (A341's TRP side chain: 15.4 A). For apo/holo,
which leave *different* residues unresolved (58 rebuilt vs 36), that makes the
model error asymmetric between the two systems being compared.

### What was built

codex's design call, taken: keep the self-template repair and override
`select_atoms()` as well as `select_loop_atoms()`, so the *base* comparative
model is restricted to the gaps plus MODELLER's own `insertion_ext=2` anchor.
Restricting only loop refinement leaves the whole-structure rebuild in place --
by then it has already happened. A post-hoc splice was rejected (the backbone
next to a gap moves 1.22 A, so putting it back detaches the loop built against
it); PDBFixer + loop-refinement-only was right in spirit but loses the alignment
gaps that say what to refine.

Around that: DISU patches addressed by **model position** (author numbering does
not exist during modelling), positions resolved by walking the target alignment
(`resnum - first_observed` is right on 9UT9 and silently wrong with an insertion
code or a numbering jump), explicit SG-SG restraints (the patch alone was not
enough), and a postcondition on the output -- every declared pair, 1.8-2.3 A, not
just the ones near a gap. Insertion codes now travel end to end: detection,
SSBOND reading, CYX naming, the topology bond, and the "one sulfur, one bond"
check all key on `(chain, resnum, icode)` through one shared resolver.

### Result

| | apo prep_017 | holo prep_009 |
|---|---|---|
| declared disulfides at bonding distance | 17/17 | 17/17 |
| A363-A366 | 2.05 A (was 11.65) | 2.03 A |
| observed heavy atoms, rigid motion removed | max 0.0008 A | max 0.0008 A |
| built loop vs partner chain, <2.5 A | 0 | 0 |
| built loop vs ligand | — | 8.19 A |

Rebuilding apo with the new code and diffing against the old node atom by atom:
identical except 19 terminal-cap methyl hydrogens (free rotation, physically
equivalent) and one HIE tautomer hydrogen. The protein itself did not move.

### Measurement notes worth keeping

Comparing "did the observed atoms move" is harder than it looks, and getting it
wrong in either direction is easy:

- A naive comparison read **2.19 A**. All of it was symmetry-equivalent atom
  renaming -- a Glu's OE1/OE2 swapping.
- After that, **0.033 A** remained. All of it was the template-frame
  superposition moving the whole molecule (0.024 degrees, 0.016 A), which changes
  no internal geometry.
- Removing the best rigid transform: **0.0008 A**, the PDB's own rounding.

So the regression test measures after removing rigid motion, with an absolute
whole-structure check alongside it -- Kabsch alone would pass a model translated
fifty angstroms.

### On the tests

Three defects in this round were introduced by the fix for the previous one, and
two of them were *silent-information-loss paths*, the same class being fixed. The
schema mismatch that dropped every DISU patch passed 51 tests. The first
coordinate-preservation test passed with `select_atoms()` deleted.

What caught these was mutation testing, on codex's instruction: delete the
implementation and confirm the test fails. Now:

- delete `select_atoms()` -> runner contract 2 fail, real MODELLER smoke 2 fail
  (coordinates move 9.2 A)
- stringify the patch indices -> runner contract 2 fail
- read only the nested pair schema -> schema handoff 5 fail
- stop forwarding pairs on the single-chain path -> 1 fail

Two claims made in this session were wrong and are worth recording as such.
"Existing studies keep working" did not hold: an artifact naming a lone A52A
failed, because an insertion code was treated as ambiguous on its own rather than
only when several residues share a number. And a mutation check reported as
passing had only exercised `_disulfide_pair_sites`, not the forwarding it claimed
to cover. Both were found by codex reading the code, not by the tests.

Three pre-existing test failures also surfaced, all from earlier fixes whose
tests were never updated: the PDB writer inventory (bug #1 added a second write),
`_reconcile_cyx_cys_in_pdb`'s return shape, and the restraint fixture still using
`topology_chain_index` after bug #3 moved to atom ranges.

## 2026-08-26 — MODELLER was rebuilding the whole structure, not just the gaps

Asking for all disulfides to be declared explicitly (9UTC's 17 as the reference,
since 9UT9 leaves A363/A366 unresolved) turned up two failures and then a third,
larger one behind them.

### What was measured

| bond | before | cause |
|---|---|---|
| A363-A366 | **11.65 A** | inside a rebuilt gap, so invisible to MODELLER's template-derived restraints |
| A59-A102 | **3.53 A** (4.47 A on 9UTC) | observed at 2.03 A in the deposit and pulled open anyway |

The second one did not fit "the loop was rebuilt": both cysteines are observed.
Comparing every observed atom before and after the repair explained it -- and
overturned the assumption that a repair only touches the missing residues:

| | median | max | >1 A |
|---|---|---|---|
| observed heavy atoms | 0.28 A | **15.37 A** | 559 / 3962 |
| backbone, away from gaps | 0.18 A | 2.93 A | 15 |
| backbone, within 3 of a gap | **1.22 A** | 9.65 A | 25 / 48 |
| side chains | 0.47 A | 15.37 A | 519 |

The worst was A341's TRP side chain at 15.4 A -- the residue immediately before
the 342-366 gap. MODELLER's comparative modelling does not copy template
coordinates; it rebuilds the whole model from template-derived restraints, and
the restraints are weakest next to a gap. So the experimental structure was being
perturbed everywhere, not only where it was missing.

That matters most for exactly this campaign: apo and holo leave *different*
residues unresolved (58 rebuilt vs 36), so a whole-structure re-optimisation
introduces model error that is asymmetric between the two systems being compared.

### What was done

codex's design call, adopted: keep the self-template repair and override
`select_atoms()` as well as `select_loop_atoms()`, restricting the *base*
comparative model to the gaps plus MODELLER's own `insertion_ext=2` anchor.
Restricting only the loop-refinement stage leaves the whole-structure rebuild in
place, because by then it has already happened. Two alternatives were rejected:
a post-hoc splice breaks the junction (the backbone there moves 1.22 A, so
putting it back detaches the loop that was built against it), and PDBFixer +
loop-refinement-only is right in spirit but loses the alignment gaps that say
what to refine.

Plus, for the disulfides:

- DISU patches passed to MODELLER, addressed by **model position**. Author
  numbering does not exist during modelling; it is restored afterwards by the
  template-frame step. A first attempt passed `"363:A"` as a *string*, which
  MODELLER reads as a residue identifier rather than an index.
- Positions resolved by walking the target alignment. `resnum - first_observed`
  works on 9UT9 and silently addresses the wrong residue as soon as there is an
  insertion code or a jump in the numbering — for a covalent bond that is worse
  than no patch, so anything ambiguous now fails closed
  (`modeller_disulfide_position_unresolvable`).
- Explicit SG-SG restraints, because the patch alone is not enough: MODELLER had
  patched A59-A102 itself and still returned it at 3.53 A.
- A postcondition on the output: every declared pair, 1.8-2.3 A, not just the
  ones near a gap. A59-A102 went unnoticed precisely because it was far from one.

### Result (9UT9 apo)

| | before | after |
|---|---|---|
| declared disulfides at bonding distance | 15/17 | **17/17** |
| A363-A366 | 11.65 A | **2.05 A** |
| observed heavy atoms, rigid motion removed | — | **max 0.0008 A, RMSD 0.0005 A** |
| built loop vs partner chain, <2.5 A | 0 | 0 |

The last measurement needed care. Compared directly, observed atoms still looked
0.03 A out — and a first pass read 2.19 A, which turned out to be entirely
symmetry-equivalent atom renaming (a Glu's OE1/OE2 swapping). Removing the best
rigid transform brought the residual to 0.0008 A, the PDB's own rounding: the
0.03 A was the template-frame superposition moving the whole molecule (0.024
degrees, 0.016 A), which changes no internal geometry at all. The regression test
(`internal_geometry_deviation`) therefore measures after removing rigid motion;
an absolute threshold would fail a structure whose geometry is untouched.

### Note

Three of the defects fixed in this round were introduced by the fix for the
previous one, and one -- the schema mismatch that silently dropped every DISU
patch — passed 51 tests. The postcondition above exists because of that: a
declared bond that quietly fails to form is the same class of failure as
everything else fixed today.

## 2026-08-26 — Complex-context repair: codex review found four real defects, and holo's gap sits 6.6 A from the ligand

Four defects codex found reading the implementation (not the plan -- the plan
review had passed). Two of them were holes my own fix had opened.

- **`preserve_input_protonation` broke.** The pre-pass substitutes the repaired
  PDB as `clean_protein`'s input, so input protonation states were read off
  MODELLER's output. MODELLER builds from a one-letter sequence, so every
  ASH/GLH/LYN/HID was already gone. Fixed by restoring the source file's residue
  names into the repaired chains by residue key at split time; rebuilt residues
  keep MODELLER's standard name.
- **The complex repair vanished from `confirmation_needed`.** `repairs` is built
  from each chain's own `missing_residue_repair`, and after a complex pass those
  are empty -- 58 predicted residues reached neither the warnings nor the HITL
  block. Fixed by recording the complex repair into `repairs` directly. Fixing it
  surfaced a second problem: the "gaps were rebuilt chain by chain ... without
  the partner chain present" warning was still being emitted **after a
  complex-context run**, i.e. the record said the opposite of what happened.
- **Preflight failures were hard errors.** `modeller_repair_reference_sequence_unavailable`
  is raised *before* MODELLER runs (a partner chain with no SEQRES); the
  per-chain path could still have repaired the well-described chains. Now
  deferred, with the "MODELLER ran and failed" case still hard.
- **The variant key did not match the codebase's.** Restoration rebuilt the key
  from OpenMM's interpreted residue id while the map was built from raw columns;
  a hybrid-36 `A000` returns as `10000`, so a valid structure would have been
  *rejected*. Now restored by residue **order** -- the parse happens on text
  written in the same function, so position is exact and the two-spellings
  problem disappears entirely.

Also fixed before review: the first version of the loader **failed open**. When a
name could not be restored it left the parent name in place, which hands the
force field a charged lysine wearing a LYN label. It raises now.

Worth naming the pattern: the LYN/CYM bond loss turned out to affect **three**
call sites, and the third (`protonation.py`) was surfaced only by *working around*
the bug -- pinning `A:46` with `--protonation-states` made `addHydrogens` duplicate
every hydrogen but HZ1, because an unbonded residue hides its own hydrogens. And
of the six defects in this round, three were silent-information-loss paths that
**the fix itself introduced**. Fixing this failure mode reproduces it.

### apo result (prep_010, before the review fixes)

| | per-chain | complex |
|---|---|---|
| B built loop -> chain A, closest | 0.42 A | 3.31 A |
| pairs < 2.5 A | 27 | 0 |
| A:46 / A:50 | LYN / HID | LYS / HIE (pinned) |
| LYN or CYM left | — | 0 |

Closest interface contact is VAL56 CG1 - ASN130 ND2 at 2.98 A, about 0.27 A
inside the C/N van der Waals sum -- a mild contact minimisation resolves, not the
0.42 A overlap it replaced.

### holo: the gap is next to the binding site

The complex pass fuses **protein** chains only, so sucralose is absent while loops
are built. Measured on 9UTC, distance from each gap's flanking observed residues
to the ligand's heavy atoms:

| gap | missing | to ligand |
|---|---|---|
| chain A 44->58 | 13 | **6.6 A** |
| chain A 342->357 | 14 | 21.8 A |
| chain B 356->366 | 9 | 39.1 A |

So the chain-chain problem has a ligand-shaped twin here, and it lands on the one
place this campaign cannot afford to distort: the sucralose site. Note apo and
holo do not share gap positions (apo rebuilds 58 residues, holo about 30), so the
two systems' modelled regions are not the same set.

Plan: build holo, then **measure the built loop against the ligand** rather than
pre-emptively restructuring. `modeller_from_alignment` already exposes `hetatm`,
so including the ligand in the template is available if the measurement calls for
it.

## 2026-08-26 — pdb2pqr's LYN/CYM silently lose every bond, and PROPKA reads modelled coordinates

Two findings from the same failure, worth keeping apart.

### LYN/CYM lose their bonds (MDClaw defect, fixed)

`openmm.app.PDBFile` aliases most Amber protonation-state names back to a parent
it knows -- `HID`/`HIE`/`HIP` -> `HIS`, `CYX` -> `CYS`, `ASH` -> `ASP`,
`GLH` -> `GLU`. **`LYN` and `CYM` have no alias.** PDBFile keeps the name, finds
no residue definition, and builds *no bonds at all* for the residue: not the
peptide bond to the residue before it, and not its internal ones. The bond to the
*next* residue survives, because that one is declared by the next residue's own
`-C` entry -- which is why the damage looks one-sided. Measured on a 3-residue
ALA-X-ALA probe:

| pdb2pqr name | name after load | bonded to previous |
|---|---|---|
| HID/HIE/HIP | HIS | yes |
| CYX | CYS | yes |
| ASH / GLH | ASP / GLU | yes |
| **LYN** | **LYN** | **no** |
| **CYM** | **CYM** | **no** |

The force field then rejects the residue *before* the variant with "the set of
externally bonded atoms is missing 1 C atom. Is the chain missing a terminal
capping group?" -- pointing at the chain terminus when the cause is residue 46 in
the middle of it. Code `terminal_cap_hydrogen_completion_failed`.

`pdb_utils.py:190-200` already stated the alias fact correctly. What was never
followed through is the consequence.

**Fix** (`terminal_caps.py:_load_pdb_with_variant_bonds`, used at both PDBFile
read sites): parse under the parent name so every standard bond is built, then
restore the variant name on the Topology *before the force field sees it*. The
ordering is the whole point, and the obvious version is wrong -- measured:

| approach | bonds | protonation |
|---|---|---|
| as-is | internal 0, peptide 1 of 2 | — (fails) |
| rename, restore name *after* output | correct | **HZ1 added: neutral Lys becomes charged** |
| add the peptide bond only, keep name | internal still 0 | — (fails) |
| **rename -> load -> restore name -> force field** | internal 20, peptide 2 of 2 | **unchanged** |

ff19SB carries real `LYN`/`CYM` templates, so once the name is back the match is
exact and no hydrogen moves. `Topology.loadBondDefinitions` was rejected: it
mutates process-wide class state and would need every internal bond redeclared.
Confirmed independently by codex on the same file, same numbers.

### PROPKA is reading MODELLER's coordinates (open, not a code defect)

pdb2pqr runs *after* the missing-residue repair, so PROPKA assigns pKa using
predicted loop geometry. This is measurable. Of the 38 residues MODELLER built in
9UT9 chain A, exactly two carried non-standard protonation, and **both flipped**
when the loop was rebuilt in complex context instead of chain by chain:

| residue | per-chain repair | complex repair |
|---|---|---|
| 46 | LYS (+1) | LYN (0) |
| 50 | HIE | HID |

A formal charge moved by 1 on a residue whose coordinates are entirely predicted,
which propagates to the neutralising ion count. PROPKA is not measuring the
protein there; it is measuring the model. Lys pKa is ~10.5 -- a neutral lysine at
pH 7 needs an extreme environment, and this one sits in a freshly built loop.

Running PROPKA *before* the repair was considered and rejected: it does not remove
the bias, it inverts it -- observed residues flanking a gap are then evaluated as
far more solvent-exposed than they are. pdb2pqr also places hydrogens, not just
predicts pKa, so splitting the two is real surgery.

Taken instead: pin the variant calls that sit on predicted coordinates to standard
states with the existing `--protonation-states`, recorded in the node label and
conditions. No pipeline change, and the override is visible in provenance. For
apo that is `{"A:46": "LYS", "A:50": "HIE"}`. Holo needs the same check against
its own gap positions.

Note `--conditions` will not carry a free-text rationale: the node contract checks
that every declared condition was actually applied by the tool, and rejected
`protonation_pin_rationale` with `node_execution_context_invalid`. Correct
behaviour; the rationale belongs here and in the label.

## 2026-08-26 — Missing loops at a chain-chain interface were built through the partner chain

Fixing bug #5 (MODELLER repair rejecting its own model) made the apo TAS1R2-TAS1R3
prep succeed: 58 internal residues rebuilt, 16 disulfides kept, both chains gap-free
and correctly author-numbered. Each chain was right on its own. The merged complex
was not.

`prepare_complex` repairs chain by chain (`prepare_complex.py:1808` loops over
`split_result["protein_files"]` and calls `clean_protein` inside the loop), so
MODELLER never sees the partner chain. Chain B's rebuilt 48-52 loop was modeled
straight into chain A:

- B LEU51 CD1 -> A LEU156 CB: **0.42 A** (CD2 0.57, CG 1.06)
- B LEU51 O -> A LEU156 CA: 2.25 A -- backbone, so side-chain repacking would not
  have fixed it
- 27 heavy-atom pairs under 2.5 A, 130 under 4.0 A

Nothing caught it. `merge.py` and `prepare_complex.py` contain no clash check at
all; the only signal was a warning saying loops "were modeled without the partner
chain present", which is true whether or not a clash resulted. Note this is a
missing feature, not the silent-wrong-value family of bugs #1-#4.

**Fix: repair the whole complex in one MODELLER pass.** The assembly is not
inferred -- it is the caller's own `--select-chains`, i.e. the chains about to be
merged. Measured on 9UT9 apo, per-chain vs complex-context:

| | per chain | complex |
|---|---|---|
| B built loop -> chain A, closest | 0.42 A | 4.48 A |
| pairs < 2.5 A | 27 | 0 |
| pairs < 4.0 A | 130 | 0 |
| A built loop -> chain B, closest | 6.31 A | 4.34 A |

Author numbering still restored exactly (A 26-553, B 23-556).

Most of the lower stack was already multi-chain ready and this was not obvious:
`genesis/modeller.py:124` already skips `/` chain separators when mapping model
residues back to template numbering, and `_validate_modeller_repair_model`
already keys on `(chain, resnum, icode)` across all chains. What actually blocked
it was small and specific:

- `structure:...:FIRST:@:LAST:@` does **not** span chains. `@` stops at the first
  chain break, so MODELLER read 490 residues against a 1004-residue alignment and
  rejected it. A multi-chain repair must name the chains: `FIRST:A:LAST:B`.
- `_template_alignment_row` accumulated a global residue offset across chains but
  emitted no `/` separators.
- `clean_protein` hard-failed on `len(sequences) != 1`.

Deliberate choices worth recording:

- The PDBFixer-vs-MODELLER threshold is still applied **per chain**; the complex
  pass runs when any one chain resolves to MODELLER. Counting gaps over the fused
  complex would have changed which structures escalate.
- The pre-pass **defers** to the old per-chain path when it cannot probe or fuse
  (unreadable coordinates, duplicate chain ids) rather than failing the run. It
  runs before any per-chain error handling exists, and a first attempt turned a
  per-chain failure into a whole-run crash -- caught by
  `test_failed_protein_chain_blocks_overall_success_and_partial_merge`.
- A MODELLER repair that ran and *failed* is a hard error, never a silent
  fallback.

Caveat: this is correct when the selected chains are the biological unit. Select
a crystal-contact neighbour and MODELLER will now respect that contact when
building loops. Still strictly better than building through it, but not free.

## 2026-08-26 — Choosing which mdclaw a compute node runs, and two ways I got it wrong first

Closed the last of the four: the sbatch `submit_job` generates bound the repo
but never put it on `PYTHONPATH`, so host-side SLURM tools ran the checkout while
the payload they submitted ran `/opt/mdclaw`. Measured on the holo system:
checkout 8153 restrained atoms, baked package 4041. The restraint fix from
earlier the same day had never reached a compute node.

The obvious fix -- mirror what `bin/mdclaw` does and always overlay -- is wrong,
and the repo owner caught why: **a general user has no repo to bind.** A pip or
conda install keeps its package in `site-packages`, and binding that into the
container would replace the image's dependency layer with the host's.
`bin/mdclaw` never hits this because `bin/mdclaw` only exists in a checkout or a
plugin; its overlay contract was never general.

So: `configure_container --source-mode`, `image` by default (today's behaviour
exactly, and nobody without a checkout is affected), `overlay` opt-in. Detection
of a valid overlay root is the directory holding both `bin/mdclaw` and
`mdclaw/__init__.py` -- not `.git`, which excludes plugin installs, and not
`pyproject.toml`, which is not guaranteed in one.

Two things I got wrong and had to be told:

**Storing the resolved root in the config recreated the same bug in a new
shape.** Write the config from checkout A, submit from B: sbatch binds A while
the login-side tool runs B. The root has to be resolved per submission and only
the mode stored. `configure_container` also stopped rejecting overlay on local
ineligibility, because the config may legitimately be written on a machine with
no checkout and submitted from one that has it.

**Resolution ran even when it could not matter.** An explicit `environment`
takes precedence over container execution, so a job with `environment="module
load ..."` never enters the container -- but an overlay setting left in the
config rejected it anyway. Gated on the same `container and not environment`
condition the sbatch generator uses.

Also worth recording: my first test for that gate **passed with the gate
removed**. `submit_job` returns `tool_not_available` before reaching the
container block when sbatch is absent, which it is inside the container where
the suite runs, so the assertion was vacuous. Fixed with the mocking pattern
tests/test_slurm_server.py already uses, plus a positive control asserting
submit_job actually reaches the resolution.

That is the third time this session a test passed while pinning nothing. The
only thing that caught any of them was breaking the fix on purpose and checking
the test noticed. Mutation-check anything whose whole job is to catch a silent
failure.

---

## 2026-08-26 — The cap fix was itself silently wrong, in the way I said I was avoiding

Reviewed the terminal-cap fix with a second agent before committing it, and the
review found the fix had the same shape of defect as the bug it closed.

Completing the cap hydrogens means loading the structure into OpenMM, and
OpenMM's PDB loader normalises Amber residue variants on the way in: CYX->CYS,
ASH->ASP, HID/HIE/HIP->HIS. Writing that back out handed pdb2pqr a structure
whose cysteines were no longer the CYX this prep had decided on. Measured on
9UT9 chain A: the file given to pdb2pqr had **CYX 0 / CYS 14** where the input
had **CYX 14 / CYS 0**.

The campaign's prep still produced all 16 disulfides -- but only because pdb2pqr
re-derives them from SG-SG geometry on its own. Accidental, not by design. An
explicit `--disulfide-pairs` choice, or a bond a deposit declares that geometry
alone would miss, would have been discarded in silence. Exactly the failure mode
I had cited when rejecting the strip-and-reattach alternative.

The repo already had the guard: `restore_resnames_by_residue_key`
(`mdclaw/structure/pdb_utils.py`, restores by residue key rather than atom
index), which the *post*-pdb2pqr cap helper already calls. Only the new
pre-pdb2pqr helper did not. Fix was one call plus a hard failure when the
restore cannot be applied.

Two other things settled in the same review:

**Fail-soft was worse than useless.** Passing the original file through on a
completion failure only reproduces the pdb2pqr abort under
`protonation_method_failed`, hiding the cause. Now returns
`terminal_cap_hydrogen_completion_unavailable` (force field XML unresolved) or
`terminal_cap_hydrogen_completion_failed`, and the call site returns before
running pdb2pqr at all. The narrow behaviour change: a cap arriving **complete
and already AMBER-named** would have passed pdb2pqr unaided, so for that input
plus an unrelated helper failure this is stricter than before. It fails loudly
with a specific code, which is the right trade.

**The post-pdb2pqr helper is not redundant and must not be replaced.** It looked
like dead weight once the pre-helper existed -- it adds zero atoms on that path.
It is in fact the reverse name normalisation: pdb2pqr emits AMBER cap names
(`CH3`, `HH31`...), and the post-helper's Modeller round-trip is what turns them
back into the OpenMM-canonical names (`C`, `H1`...) that the published
`merged.pdb` carries. Verified by tracing ACE atom names through every stage.
Replacing it with a cheap validator would have leaked `HH31/HH32/HH33`
downstream.

Cap routes now covered by tests, all measured rather than assumed: cap arriving
complete (OpenMM names and AMBER names, neither duplicated), cap arriving
half-finished, cap on one terminus only, and `strip_input_terminal_caps` first
(helper skips, pdb2pqr gets the original). One exploratory failure -- a one-sided
ACE cap whose C-terminus lacked OXT -- is a malformed fixture, not a regression:
PDBFixer emits OXT on a free C-terminus whenever `add_missing_atoms=True`, and
the same structure with OXT passes.

Worth carrying forward: the first three bugs this session were found by
comparing two systems that should have matched. This one was found by having
something else read the fix. Neither would have surfaced from the tests passing.

---

## 2026-08-26 — pymbar 4.2 FES histogram: three edges worth knowing

MDClaw has no MBAR tool, so the TAS1R umbrella analysis went through pymbar
directly (`scripts/umbrella_mbar.py`). Smoke-tested on ten throwaway pilot
windows before the real grid finished, which was the point -- all three of
these fail at `get_fes`, hours after the sampling is already paid for.

1. `generate_fes(fes_type="histogram")` runs `np.shape()` over
   `histogram_parameters["bin_edges"]`. Two axes with different bin counts make
   that list ragged and numpy raises "inhomogeneous shape". Both axes need the
   same bin *count*; the widths may still differ.
2. `get_fes` looks every query point up in `histogram_data["bin_label"]`, which
   only holds bins that received samples. A full mesh of bin centres therefore
   raises `KeyError` on the first empty bin. Query `bin_label`'s own keys and
   leave the rest NaN.
3. Binning is `np.digitize(x, edges) - 1`, so a sample sitting exactly on the
   top edge lands in bin index `nbins` -- one past the end -- and pymbar keeps
   it, because it only rejects negative indices. Building edges as
   `linspace(x.min(), x.max(), nbins+1)` guarantees one such sample. Pad the
   outer edges by a hair.

Also worth recording: the PythonTorchForce bias costs a clean 2.04x on this
system (109.4 ns/day biased against 222.8 unbiased, 358k particles, one GB200).
The bias touches 3186 atoms but the force moves all 358k positions into torch
every step, so the penalty is set by system size rather than by CV group size.
Budget umbrella campaigns at half the unbiased rate.

---

## 2026-08-26 — SLURM payloads run the container's baked package, not the checkout

The restraint fix above was made, tested, and then had no effect on the rerun:
holo `eq_002` came back with the same 4041. `bin/mdclaw` deliberately binds
`PKG_ROOT` and exports `PYTHONPATH` so "the container runs the same mdclaw
source as the host-side native tools (the image's baked package is only the
dependency layer)". The sbatch script `submit_job` generates does not:

    singularity exec --nv --bind <repo>,<job_dir>,<out_dir> <sif> mdclaw ...

The repo is bound but nothing puts it on `PYTHONPATH`, so the payload imports
`/opt/mdclaw/lib/python3.12/site-packages/mdclaw`. Host-side SLURM tools run the
checkout; the compute-node payload they submit runs the image. That is exactly
the drift `bin/mdclaw`'s comment says it exists to prevent, and it is invisible
unless you diff the two.

Checked rather than assumed: `diff -rq` between the baked package and this
checkout reports only the four files edited this session, so the shared rikyu
SIF is otherwise HEAD and nothing else silently differed for the SLURM stages.

Not fixed here -- changing sbatch generation mid-campaign, or rebuilding and
overwriting a SIF shared out of /data1, are both bigger than the problem.
Sidestepped instead: `--restraint-atoms heavy` reaches the same atoms through a
code path the bug does not touch. Verified against the baked package on both
systems: apo `heavy` and `solute_heavy` select the *identical* 7968 atoms
(so the completed apo min/eq needed no rerun), while holo `heavy` gives the
correct 8153 against `solute_heavy`'s 4041. holo was rerun a second time as
`min_003`/`eq_003`/`prod_003`.

The restraints.py fix stays in the tree as the general fix, but it will not run
on a compute node until the image is rebuilt.

---

## 2026-08-26 — Restraints addressed the wrong chains, and only after prep got better

Same TAS1R2-TAS1R3 campaign, found by comparing the two systems' equilibration
metadata rather than by anything failing. apo restrained 7968 solute heavy
atoms; holo restrained 4041 and split them as `{protein: 4039, ligand: 2}` --
2 restrained atoms for a 23-heavy-atom sucralose is not a plausible number, and
4041 turns out to be exactly TAS1R2 chain A's heavy-atom count. TAS1R3 chain B
(4089 heavy) and the ligand were never restrained during either min or eq.

`select_restraint_atoms` addressed prep's solute components by
`topology_chain_index` into the built topology's chain list. That index is only
valid if topology generation preserves prep's chain decomposition, and it does
not: when Pablo identifies every residue it emits each ACE/NME cap as a chain of
its own, so holo's chains 0/1/2 are ACE, the chain-A body, and NME -- 3 + 4036 +
2 = 4041, labelled from prep's components as protein/protein/ligand. apo was
correct only by accident: Pablo could not parse it, the PDBFile fallback kept
whole chains, and chain index 0/1 really were the two proteins.

The sharp edge is that the same prep gives two different chain layouts
depending on whether an *unrelated* part of the file parsed. Fixing the
sucralose residue identity earlier the same day is what let Pablo succeed on
holo, which is what moved the caps into their own chains, which is what
misaligned the restraints. A fix in one place silently changed the meaning of an
index in another.

Fixed by addressing components through their prep atom-index range instead --
solute atoms keep prep's order and lead the topology, solvent and its virtual
sites are appended -- with a warning if a component range reaches solvent or
runs past the end of the topology, so the assumption fails loudly if it ever
stops holding. After: apo unchanged at 7968, holo 8153 = protein 8130 + ligand
23. holo min/eq/prod were rerun from `topo_002` as `min_002`/`eq_002`/`prod_002`;
the first holo production was cancelled rather than kept, because a comparative
PMF cannot have the two states equilibrated under different protocols.

Worth generalising: all three bugs this session were silent. Nothing raised, no
guardrail fired, and each returned a plausible-looking number. The counts that
exposed this one were only visible because two systems that should have matched
were compared side by side.

---

## 2026-08-26 — Two ways preparation lost a molecule it had already built

TAS1R2-TAS1R3 campaign (9UT9 apo / 9UTC sucralose). Two independent prep
failures, both of the same shape: preparation did the chemistry correctly and
then wrote it out in a form the next stage could not read.

**Terminal caps never reached the protonation baseline.** `prepare_complex
--n-terminal-cap ACE --c-terminal-cap NME` failed with
`protonation_method_failed` on any capped chain. PDBFixer inserts caps as heavy
atoms only -- ACE gets C/O/CH3, NME gets N and the methyl carbon under the name
`C` -- and pdb2pqr has no topology entry for either residue, so it cannot
complete their hydrogens. It charges the atoms it does match from AMBER.DAT,
gets ACE -0.3369 and NME -0.4157, finds the total non-integral, and aborts the
whole structure. The cap-hydrogen completion MDClaw already runs
(`_complete_terminal_cap_hydrogens_with_modeller`) sits *after* pdb2pqr, so it
never got the chance. Fix: complete only the cap hydrogens before pdb2pqr and
write the cap atoms under pdb2pqr's own AMBER.DAT names (`CH3`, `HH31`...).
Each cap then sums to zero. Measured on 9UT9 chain A: +7 atoms, 7 renamed, zero
hydrogens added outside the caps, and pdb2pqr correctly leaves ASP A 26 as a
plain `N, H` amide rather than building an NTER onto a capped terminus. OpenMM's
PDB loader maps the AMBER cap names back on load, so nothing downstream
changed.

**A two-residue ligand was written as two residues.** Sucralose is deposited as
RRY + RRJ joined by a declared covalent bond (RRY O2 - RRJ C1, 1.407 A).
`clean_ligand` built it correctly as one molecule -- C12H19Cl3O8, 42 atoms, one
fragment, two rings, pose preserved to 0.0000 A -- then wrote a PDB carrying one
residue *name* over the two original residue *numbers*, because it set the
chain and residue number only on atoms that arrived without PDBResidueInfo.
Everything downstream reads that as separate residues. Pablo matched none of
them and fell back to PDBFile; the fallback's own repair,
`_patch_ligand_molecule_internal_bonds`, looks for a single residue whose atom
count equals the molecule's and found none; so the ligand reached
`create_system` with no bonds at all and failed as "No template found for
residue 1030 (RRJ)". Unifying the residue number exposed the second half: both
sugars name their atoms C1..C6 / O2..O5, and a PDB reader keys atoms by name
within a residue, so OpenMM silently dropped 9 of the 42 atoms on load. Fix:
unify chain/residue number across every atom and hand each colliding name a
name no other atom wants. Verified end to end with CONECT records stripped, the
state after solvation: 42 atoms in one residue, 43 bonds patched from the
molecule, `SystemGenerator.create_system` OK.

Worth noting for future ligand work: neither failure was loud. `clean_ligand`
returned `success: true` in both the broken and the fixed case; the only signal
in the broken one was a warning that template matching had fallen back, and a
`smiles_used` field describing a 12-heavy-atom fragment next to a
`num_heavy_atoms: 23` result. Left to itself the tool had fetched the CCD SMILES
for RRY alone. Passing the full sucralose SMILES explicitly is what made the
chemistry match the file.

**Unrelated, recorded for the campaign:** MODELLER is unlicensed in the shared
rikyu SIF (`KEY_MODELLER10v8` unset), and PDBFixer's repair scope is 10 internal
missing residues / 5 per segment. 9UT9 has a 25-residue gap, so neither route is
available and the disordered loops were left unbuilt
(`--missing-residue-method none`). ff19SB template matching still passes and
pdb2pqr leaves the break points as neutral nicked backbone -- no artificial
buried charges -- but the fragments are held together only by the fold.

---

## 2026-08-25 — Solvation was changing what element an atom is

Campaign task `041_ligand_4erf` lost its first `topo` node in all three
replicates to `openmmforcefields_build_failed`, "No template found for residue
92 (0R3)". Every retry succeeded. That looked like nondeterminism and it was
not: all three agents had edited their completed parent's `solvated.pdb` in
place before retrying. Artifact mutability masquerading as nondeterminism --
the node id stayed the same, its bytes did not.

What they edited was two element fields. packmol-memgen's
`MembraneParams.pdb_reindex` right-aligns any three-character atom name and
writes `atomname[0]` into the element column:

    line = line[0:6] + "...{:>2}\n".format(..., segid, atomname[0], align=ali)

So `CL2` comes back as carbon. Deterministically, and for every two-letter
element written under a three-character name: `CL*` to C, `BR*` to B, `ZN` to
Z, `MG` to M, `FE` to F, `NA` to N. MDClaw's solute restore deliberately left
atom names and elements as the writer wrote them, so the corruption survived
solvation and `build_amber_system` copied it into `system.prepared.pdb`.

Diagnosis by experiment, not inference: the identical DAG was cloned twice, the
two element fields changed in one copy and nothing else -- 2 lines of 71134 --
and the real `build_amber_system` run on both took the node from `failed`
(`openmmforcefields_build_failed`) to `completed`, with `system.xml`,
`topology.pdb`, `state.xml` and a minimisation report. Two earlier diagnoses
were wrong and are recorded because both were plausible: a transient Pablo CCD
auto-download failure (contradicted by the failure record, which contains the
CCD definition and rejects it for "wrong number of atoms"), and a later reader
inferring the element from PDB columns 13-14 (packmol-memgen writes the wrong
element itself; nothing downstream infers it).

`restore_solute_identity_by_prefix` now restores the element too, per atom and
only where the source and target atom names agree. That rides on the check the
overlay already made -- source residue i's heavy-atom tuple must equal target
residue i's -- so it adds no new risk, and the appended solvent, which has no
source atom, is untouched.

Scope: an audit of 102 solvated files found the two 0R3 chlorines plus ZN
written as Z three times and CA as C three times. Bare monoatomic ions are
repaired downstream by the ion sanitiser, which is why nothing had noticed; a
metal or halogen *inside* a ligand or cofactor is not, and topology validation
checks atom counts, energy, disulfides and protonation but not element
preservation. So this could have produced a scientifically wrong system that
built cleanly, rather than one that failed loudly as 041 did.

Not fixed here: agents mutating a completed node's artifacts. This is the
second instance -- the first rewrote `mdclaw/solvation/water.py` mid-campaign --
and in both the agent was correct about the underlying bug. Terminal nodes are
supposed to be immutable evidence.

---

## 2026-08-25 — A declared condition was a contract nobody could read

`create_node --conditions` declares a JSON dict, and
`validate_node_execution_context` fails the node when the stage tool does not
report a declared key back in `actual_conditions`. A failed node is terminal.
Sixteen of 300 campaign attempts lost a node this way -- 12 prep, 5 prod, 1
solv. The keys were semantically right and lexically wrong: `chains` for
`select_chains`, `ligands` for `include_ligand_ids`, plus `residue_ranges` and
`ligand_net_charge`, which have no counterpart at all. One agent recovered by
recreating the node with `conditions: {}`, throwing the DAG's record of intent
away to get past a naming problem.

Nothing could have told it otherwise. `skills/md-prepare/` never mentioned
`--conditions` (grep count: 0) while md-equilibration and md-production both
show examples, so the habit was taught without the vocabulary. `explain_node`,
the skill's designated pre-flight, sets `validate_conditions` only when
`--actual-conditions` is passed, which no skill mentioned -- and it compares
two caller-authored dictionaries, so echoing the same bad key through it
green-lights the node that later dies. Measured, not inferred.

Two attempts at a fix were wrong and are recorded because the second was worse
than the problem.

The first put the rule in `skills/common/run-loop.md`, telling the agent to
confirm keys with `mdclaw --list-json <tool>`. `prepare_complex` advertises 37
parameters of which 12 are not conditions, and `residue_ranges`,
`disulfide_pairs` and `build_terminal_missing_residues` are all among the 12 --
three of the nine keys the campaign actually got wrong. The text would have
formalised the bug, and it also presented `--actual-conditions` as a pre-flight
guarantee it does not provide.

The second tabulated `ACCEPTED_CONDITION_KEYS` per node type from the
`actual_conditions` literals by AST and had `create_node` reject anything
outside it. The AST walk missed `build_amber_system`, which passes
`actual_conditions` through a helper, so the topo vocabulary silently lost
`forcefield`, `water_model`, `nucleic_forcefield`, `glycan_forcefield` and
`is_membrane`. `create_node --node-type topo --conditions
'{"forcefield": "ff19SB", "water_model": "OPC"}'` -- the most ordinary topology
declaration there is -- was refused, and misdirected to `forcefield_xml`, the
OpenMM builder's key. The full suite passed throughout: no test covers topo
conditions. That is the failure this was meant to prevent, pointed the other
way, and it is why the registry is gone rather than patched. Any table is wrong
by construction: several tools serve one node type and accept different keys,
so a per-type table is too strict for one and too loose for another, and a
hand-written one drifts silently.

What landed is smaller. At the point of failure the executor is known exactly
and its vocabulary is simply the keys of the `actual_conditions` it just
reported, so no table is needed. `condition_hints.py` takes that set as an
argument and knows nothing about node types.

Similarity alone cannot rank the suggestions: `chains` scores 0.632 against
`select_chains` while `mutations` scores 0.696 against the unrelated
`max_iterations`, so every cutoff that keeps the good suggestion keeps the bad
one first. What separates them is whether the noun survives the rename, so a
candidate qualifies only by containing the key's stem or being a near-spelling
of the whole key. `chains` now yields `select_chains` alone, `ligands` yields
all four honest readings, and `residue_ranges`, `ligand_net_charge` and
`mutations` yield nothing, which is the correct answer for a key with no
counterpart.

All three condition errors now carry a remedy, not just `condition_missing`,
and keys reported as `None` are left out of the "cross-checked" list because
they are rejected as unverifiable -- advertising them would send the caller
back into the same failure. `explain_node` reports `conditions_checked` so
`ready_to_run` no longer implies a guard that did not run. The skill paragraph
is scoped to prep/solv/topo/min/eq/prod: `analyze` requires
`analysis_data_scope` at creation and has no runtime cross-check at all, so the
universal phrasing was false for it and contradicted md-analyze's own
mandatory example.

Open, and deliberately not attempted here. The remaining defect is that none of
this is learnable before the first failure: a successful run exposes the
vocabulary nowhere -- not in `node.json`, not in the tool summary -- so
"declare only keys you have seen a tool report" is circular advice, and on
first use it amounts to "declare nothing", which is the behaviour that lost the
intent record in the first place. The sound fix is exact `condition_keys`
metadata on each `@node_tool`, exposed through `--list-json`, checked by the
CLI before it invokes the tool it has already selected (so a rejection costs no
node), and asserted at run time against `set(actual_conditions)` so the
metadata cannot drift the way the registry above did.

---

## 2026-08-25 — `--salt` gated neutralization it does not control

A benchmark agent running `049_nucleic_1iv6` could not get a DNA duplex to come
out neutral, traced it to `solvate_structure`, and patched one line. The
diagnosis was right and is now in, with the surrounding semantics corrected.

`auto_charge_delta_applied` was `bool(salt and auto_charge_delta)`. packmol-
memgen's own source settles what `--salt` means: `main.py:406` warns that
without the flag "only neutralizing ions will be added", `--nocounter` is the
documented way to suppress counterions, and the neutralizing count at
`main.py:1462` divides by `ion_dict[salt_c]` unconditionally. `--salt` asks for
bulk salt; counterions are added either way, sized from memgen's own -1 per
nucleotide guess. Gating the curated true-minus-guess correction on `--salt`
therefore disabled it exactly on the neutralize-only path.

Measured on 049: delta `+2` unapplied gave 26 K+ and a topology at `+2 e`;
applied gave 24 K+ and `~5e-11 e`, and the scorer's `system_is_neutral` check
passed. Not a single-system artefact — campaign attempt `051_nucleic_1kx5` r1
ran `salt=false`, left the correction unapplied, and built a topology at almost
exactly `+2 e`. Attempts that chose `salt=true` were never affected, which is
why 049 r1 and r2 passed on the old code.

Three adjacent defects shared the mistake and are fixed with it:

- `neutralization_expected` was `bool(salt)`, so `build_amber_system`'s
  `neutralization_charge_mismatch` guard switched itself off precisely where it
  was needed. MDClaw exposes no `--nocounter`, so counterions are always added
  and the flag is now `True`.
- `--salt_c`/`--salt_a` were passed only under `if salt:`. memgen defaults
  `salt_c` to K+ while `solvate_structure` documents Na+, so neutralize-only
  runs silently ignored the caller's ion choice. The OpenMM fallback already
  passed `positiveIon`/`negativeIon` unconditionally; the memgen path was the
  inconsistent one.
- `embed_in_membrane` repeated the same `if salt:` gate around the charge-delta
  computation, the applied flags, and the ion-species arguments.

Ion species is not scored by MDDataBench — `composition.py` treats NA and K
alike as solvent — so the K+ to Na+ change is a correctness fix, not a
benchmark effect.

Four regression tests cover what the old ones missed: the existing coverage
only ever exercised `salt=True`. The new DNA and RNA fixtures put O5' but no P
on the 5' residue, so they model the absent terminal phosphate rather than
asserting a number against a chemically impossible strand. All three behaviour
tests fail against the pre-fix code; the protein-only control passes either
way, as it should.

Provenance note: the original one-line change was authored by a `pi`/kimi-k3
benchmark agent editing the shared checkout mid-run, not by the campaign
operator. Only `049_nucleic_1iv6` r3 ran against the modified source — 006 uses
the membrane path, 021 had `delta=0`, and 049 r1/r2 used `salt=true` — so the
validation run's conclusions stand.

---

## 2026-08-25 — Protonation contract, actionable guardrails, and topology metadata made explicit

Protein preparation now has two independent controls. `protonation_method`
selects a `standard` or `propka` pdb2pqr baseline, while
`preserve_input_protonation` optionally overlays deposited ASH/GLH/LYN and
histidine variants. It defaults false, so standard really is all-standard.
CYX disulfides and metal-site CYM remain structural chemistry, and explicit
site overrides are a final overlay whose provenance no longer replaces the
baseline label. A requested baseline now fails closed when pdb2pqr is missing
or fails; stale conventional output files are deleted before each attempt so a
failed retry cannot report an earlier PDB as success.

End-to-end SIF probes established the chemistry rather than only mocking the
wrapper. BPTI retained all three disulfides as CYX without SG hydrogens. 1AY7
under the standard baseline had charged Asp/Glu/Lys/Arg hydrogen patterns,
neutral HID/HIE and free CYS, and no HIP; CYX again had no SG hydrogen.
An ASH input became charged ASP with preservation off, and remained ASH with
HD2 plus explicit overlay provenance when preservation was on.

Every structured blocking guardrail now carries a local `suggested_fix`, with
an AST regression test enforcing that rule. The ff19SB/TIP3P refusal names both
exits: change water to OPC, or keep TIP3P and change protein force field to
ff14SB. Failure manifests preserve these local remedies. Topology completion
now requires a readable `amber_metadata.json` with parameters and force-field
provenance. Both Amber and custom-OpenMM builders emit and return it; the latter
uses the historical filename without mislabelling custom XML as Amber.

Validation included 371 focused protonation/guardrail/node/topology tests and
the two real node-mode production smoke tests. The broad non-pipeline run first
reported 1630 passed and only those two old hand-built topo fixtures failing;
after the fixtures copied the builder's metadata, both passed. Changed-file
ruff and `git diff --check` pass.

## 2026-08-25 — Visual QA turned off by default: unrequested previews were half of all input tokens

Measured during the 300-attempt MDDataBench campaign (`pi` + `rikyu/kimi-k3`,
`cli_skill_sif`). Across 153 completed attempts, 89% of transcripts contained
at least one base64 PNG, and those images accounted for **50% of all input
tokens**: 21.3 M tokens/attempt with them against 10.7 M without.

The cause was not an MDClaw tool returning images. It was `skills/common/visual-qa.md`
instructing "render a preview after every stage that changes the system", so
MDClaw wrote 741 preview PNGs (median 954 KB, max 3.2 MB, 656 MB total) and the
agent then opened them with the harness `read` tool. Every opened image is
re-sent on every later turn, so one 954 KB preview read early costs roughly
80x its size over an 80-turn attempt.

`rikyu/kimi-k3` declares `input: ["text"]` — it cannot see images at all. Half
the input budget was being spent on data the model could not read.

Changed: visual QA is now **off by default and runs only when the user asks**.
Edited `skills/common/visual-qa.md` (canonical page, plus an explicit "never
open a preview on a text-only model" rule), and the six referring sites in
`common/run-loop.md`, `md-prepare`, `md-equilibration`, `md-production`, and
`md-analyze`. `.agents/skills` and `.claude/skills` are symlinks, so they follow.

Related measurement, same campaign: the rikyu endpoint **does** do automatic
prefix caching and reports it (`prompt_tokens_details.cached_tokens`, 20992 of
21030 on a repeat, and the prefix still hits when only the tail changes). Only
1.2% of an attempt's input is genuinely new, so a cache-capable provider bills
roughly 9x less than the naive token count suggests — 18x once previews are off.

## 2026-08-24 — 027 complex completed on Slurm and passed MDDataBench 20/20

The public-prompt-only `027_complex_1b6c` workflow prepared the requested
1B6C A/B heterodimer (107 + 326 residues), built a neutral 194,343-atom
ff14SB/TIP3P system, and completed 0.1 ns NVT + 0.2 ns NPT equilibration at
310 K and 1 bar.  Slurm job 41364 then completed a fresh 2.5 ns production as
`prod_002` on one GPU, yielding 250 frames at 10 ps and passing visual QA.

An earlier interactive `prod_001` was interrupted after 1.47 ns.  Reinvoking
the same running node restarted from the equilibration state and appended a
reset time series to its existing artifacts, so that node is deliberately not
used for evaluation and remains preserved for diagnosis.  Creating a fresh
production node from completed `eq_001` avoided the ambiguous trajectory.
The official MDDataBench result for `prod_002` is prep 12/12 and MD 8/8
(20/20), with all nine adversarial baselines rejected.  Its multimer mapping
resolved both monomers and all 1299 contract atoms; MDDataBench's current RMSF
verdict remains one pooled whole-complex profile, so per-subunit fidelity is a
benchmark follow-up rather than an MDClaw execution failure.

## 2026-08-24 — explain_node now previews explicit NMR candidate selection

The `046_nucleic_1a66` follow-up confirmed that `explain_node` passed
`actual_conditions` only to declared-condition validation, while prep input
resolution independently inspected the DAG.  For a multi-model source this
left `structure_file` unresolved and permanently reported
`source_candidate_selection_required`, even when the caller supplied a valid
`source_structure_id`; the actual `prepare_complex` path succeeded because it
had separate candidate-selection machinery.

The read-only prep preflight now uses the existing source-bundle selectors when
one of the four existing source-selection values is present in
`actual_conditions`.  A valid selection resolves the concrete candidate and
can report `ready_to_run=true`; an unknown ID remains non-ready and reports the
valid candidates.  No selection file is materialized, no CLI or `create_node`
surface was added, and selection-free behavior is unchanged.  Focused lint and
tests pass: 240 node/prepare tests plus two source-candidate server smoke tests.

## 2026-08-24 — Correction: asymmetric RMSF tolerance makes 046 pass 20/20

The 19/20 result recorded immediately below exposed an unnecessarily symmetric
MDDataBench fluctuation-magnitude band, not an MDClaw failure.  The RMSF-total
lower edge now receives 5 window SD of slack while the upper edge remains at
4 SD: too little motion is still checked, but a mildly stiff independent 1 ns
trajectory is less harmful than excessive motion or unfolding.  The unchanged
046 trajectory now passes 20/20, and its real-run plus nine adversarial
negative controls all receive the intended verdicts.

## 2026-08-24 — 046_nucleic_1a66 completes as a DNA-only 1 ns run

Task `046_nucleic_1a66` was executed from the public prompt only; hidden
`task.json` fields were first read at scoring time.  The requested system is
the two DNA strands (author chains B 315--326 and C 340--351), so the deposited
protein chain was correctly excluded.  The first of 18 deposited NMR models
was selected explicitly.  Preparation retained 24 DNA residues and 761 solute
atoms, and DNA.OL15/TIP3P produced a neutral 44,231-atom box.  Terminal
5'/3' templates correctly changed the two 12-mer strand charges to -11 each.

Minimization, 0.1 ns NVT, 0.2 ns NPT, and 1.0 ns unrestrained NPT production
completed at 300 K and 1 bar on one GPU using the normal 4 fs HMR default.
Final visual QA retained both bent DNA strands inside the periodic box with no
gross solvent or ion accident.  MDDataBench scored prep 12/12 and MD 7/8
(19/20 total); only total fluctuation missed the calibrated lower bound by
0.0161 A (1.1695 A versus 1.1855 A), while sequence, atoms, chemistry,
conditions, elapsed time, fluctuation profile, radius of gyration, temperature,
and density passed.  All nine adversarial negative controls were rejected, but
the negative-control suite reports `success=false` because it requires the real
run to pass every MD gate.

One workflow rough edge remains: `create_node` correctly requested an explicit
NMR candidate, and `prepare_complex --source-structure-id candidate_001`
succeeded, but `explain_node` still reported the candidate-selection preflight
as unresolved even when the same choice was supplied through actual conditions.

## 2026-08-24 — 037_ligand_1g74 completes with a single OLA alternate

Task `037_ligand_1g74` retained chain A residues 1--131 and oleate OLA 132,
while excluding the crystallization phosphate.  The deposited OLA has two
complete 20-heavy-atom alternates at occupancy 0.50; preparation selected all
atoms consistently from alternate A (never a mixed conformer), protonated it
as oleate with expected net charge -1, and produced the reference-matching
2,107-atom solute (2,054 protein + 53 OLA atoms).  The neutral 44,984-atom
ff99SB-ILDN/TIP3P system completed minimization, 0.1 ns NVT, 0.2 ns NPT, and
1.0 ns NPT production at 298 K and 1 bar on one GPU.  Final system-box and
ligand-site previews showed an intact beta barrel and OLA retained in its
binding cavity.

The run used 2 fs because the benchmark reference was inspected during manual
triage.  That produced a valid conservative trajectory, but it is not the
normal execution policy: future benchmark runs must derive settings only from
the public prompt and inputs.  Hidden/reference fields in `task.json` are for
scoring only; when the prompt omits the timestep, MDClaw's topology-aware
default applies (normally 4 fs for HMR).

## 2026-08-24 — Author insertion codes now survive inspection, selection, and preparation

Task `036_ligand_1ceb` exposed an author-numbering edge case: chain A begins
with observed residues `1A, 1, 2, ... 79`. Numeric tuple selection treated
`A:1A-79` as if `1A` and `1` occupied the same position and dropped the plain
residue 1; missing-residue probing then confused the extra observed insertion
with a SEQRES gap and unnecessarily escalated to MODELLER.

Range selection now resolves observed endpoints in deposited residue order, so
`A:1A-79` retains all 80 residues. `inspect_molecules` reports the ordered
author residue IDs, insertion codes, repeated author numbers, and a suggested
unambiguous span. Missing-residue classification accounts for observed
insertions when locating terminal SEQRES gaps, and `missing_residue_method=none`
is available when a caller deliberately wants to record but not rebuild
internal gaps. The focused suite passes 66 tests and ruff passes on every
changed Python file.

The same run validated the existing expected-ligand-charge path. Passing AMH
`net_charge=0` through `structure_analysis` selected Dimorphite-DL's
zwitterionic candidate from the CCD SMILES, retained 26 ligand atoms, and
recorded both the expected and molecular formal charges. The `md-prepare` skill
now gives this existing path as the concise default when a task supplies an
expected charge; no new CLI argument was added. End-to-end ff99SB-ILDN/TIP3P
production completed and MDDataBench passed prep 12/12 and MD 8/8.

## 2026-08-24 — Curated region boundaries work as analysis policy, not a new client

Shweta Kumari's W535L SMO trajectory reproduced the failure mode behind the
TM-wise RMSD/RMSF feedback. The `md-analyze` guidance now separates biological
region annotation from membrane orientation: user-supplied boundaries first,
then an appropriate reviewed curated entry, with live predictors such as PPM as
a fallback. It requires sequence-based mapping to the actual simulated chain
and an explicit disagreement report rather than a silent choice.

A forward test did not use the existing `local_to_true_W535L.json`. It fetched
reviewed UniProtKB Q99835, aligned its 787-residue canonical sequence to all 496
protein residues in the analysis topology, and mapped all 496 residues. This
recovered the hand-curated local TM/loop ranges exactly, including TM5 342-363,
ICL3 364-395, and TM6 396-417. It also retained the W535L mismatch at local
residue 478 instead of dropping it, surfaced another sequence mismatch
(canonical V329 vs local F272), and identified local 481-496 as the partial
observed portion of the curated cytoplasmic C-terminal domain.

Re-running the 200 ns trajectory with the prompt-derived ranges produced 1,000
sampled frames. All 15 arrays shared with the saved UniProt-domain RMSD result
were byte-for-byte numerically identical (`max_abs_diff = 0.0`). Relative to
the PPM-aligned saved RMSF, the curated-TM alignment changed mean RMSF from
1.827 to 1.717 A; the mean absolute per-residue difference was 0.169 A and the
maximum was 0.562 A at local residue 382. This supports a skill-only correction
for source selection and residue mapping; no UniProt-specific runtime client is
needed yet.

---

## 2026-08-24 — Final topology PDBs no longer carry CONECT records

Both topology builders now remove `CONECT` records only when serializing the
final `system.topology.pdb`. The authoritative force-bearing bond graph remains
in `system.xml`; source and intermediate PDBs retain `CONECT` so disulfides,
glycans, covalent ligands, and other prepared connectivity can still inform
System construction.

This fixes the reproduced MDAnalysis failure on Shweta Kumari's W535L membrane
system: OpenMM emitted hybrid-36 atom serials such as `A003B` in the solvated
topology's `CONECT` records, which MDAnalysis attempted to parse as decimal
integers. Removing those records made the same topology and trajectory readable
and allowed the domain RMSD/RMSF analysis to complete. Analysis operations that
need nonstandard make-whole connectivity must continue to obtain that graph
from the authoritative System rather than infer it from the PDB companion.

---

## 2026-08-24 — The solute is a prefix of a solvated file, not a set of keys

Both solvation writers append: packmol-memgen and `Modeller.addSolvent` emit the
solute as the leading records, in the input's order, and put water and ions after
it. The identity restore at both hops now matches on that prefix
(`restore_solute_identity_by_prefix`, `mdclaw/structure/pdb_utils.py`), guarded by
a per-residue heavy-atom name tuple, and restores residue name + chain + resSeq +
iCode on the leading residues only.

What it replaced, and why:

- `water.py` (OpenMM fallback) keyed the restore on (chain, resnum, icode). The
  write renumbers the solute, so the keys line up with the *wrong* residues and
  the overlay does not refuse — it applies. Ran the real hop on m01-5zk8's
  merged.pdb: as written 135/4428 solute atom names were wrong (the loader's
  HID/HIE/CYX collapse); the key overlay made it 3002/4428; the prefix restore
  makes it 0/4428, with 0/4428 keys wrong and 0 solvent records touched.
- `_restore_packmol_solute_identity` compared the element column per atom.
  packmol writes `Z` for zinc, and that one character abandoned the whole
  ~4900-atom restore in 13 of 16 real runs (`solute_identity_restore_warnings`:
  `atom 4896: ZN/ZN != ZN/Z`). That is where the deposit numbering was being
  lost: d02-6w9c merged.pdb says THR A 4, solvated.pdb said THR A 1, and
  `system.topology.pdb` (written keepIds=True) faithfully inherited it.

Swept all 14 real merged.pdb -> solvated.pdb pairs under `runs/studies2`: the
restore is accepted on 14/14, residue-name match stays 100%, and numbering goes
0/N -> N/N on the 11 that packmol had renumbered (a01 and d04/solv_001 already
had it, being 2 of the 3 runs whose old restore survived the element check).
Colliding (chain,resnum,icode) keys between solute and solvent are unchanged —
they are packmol's own, it numbers WAT from 1 inside the solute's chain letters —
except d03, 317 -> 316. Re-reading d02/solv_004's restored file with
openmm.app.PDBFile gives the same 91360 bonds, max 2.25 A, 0 solute<->solvent
bonds, 0 bonds over 3 A as before the restore; only the residue ids moved
(LYS312 -> LYS315).

Not changed, on measurement: the protonation hop (`protonation.py:660-682`) and
the terminal-cap hop (`terminal_caps.py:387`) keep `restore_resnames_by_residue_key`.
Their inputs are one molecule with a 1:1 key (0/34 and 0/30 ambiguous), the key is
doing real work there (282 residue names put back across 9229 residues), and a
prefix match is measurably *wrong* at the cap hop once caps exist at more than one
chain terminus (0/636 correct, offsets +1/+3/+5). Three hops, two correspondences,
deliberately.

Corrections to earlier notes: addSolvent does not give every water its own chain
called "A" — on OpenMM 8.5.1 it makes exactly two solvent chains whose residue ids
continue past the solute's, and keeping their ids collides nothing. The
`test_keeping_the_water_chain_ids_collides_them_with_the_solute` test built that
shape by hand and has been removed; the guard that the solvated write does not
pass keepIds stays. I also could not reproduce "49 protein-water bonds up to
135 A" by any route through `PDBFile`; long bonds come from chain segmentation
collapsing, not from colliding keys.

---

## 2026-08-21 — Membrane ions came out drifting across y, and it was the stride's arithmetic

Spotted from a preview during the 9UWI validation run: ions above the bilayer
sat to one side, ions below to the other. Real, and 9UWI only.

    solv (initial)  upper n= 75 <y>=+21.8+-3.4   lower n=119 <y>=-16.3+-3.1
    5L7D            upper n=168 <y>= -2.4+-2.5   lower n=133 <y>= -4.3+-2.7

Water was uniform in y in both (<y> ~= -2), so the ions were not following the
solvent. Na+ and Cl- drifted the *same* way (+25.0 / +18.8 upper, -13.0 / -18.7
lower), which rules out electrostatics — a field separates the species, it does
not move them together.

### Cause

`_apply_neutralizing_swap` (`mdclaw/solvation/patch_membrane.py`) sorted the
candidate waters by `(z, x, y)` and took every `stride`-th one, `stride =
len(candidates) // needed`. Three facts combine:

1. Tiling copies the patch in x and y only, so copies keep z bit-for-bit.
   Measured: 9UWI held 32126 waters at only 3597 distinct z, 100% of them
   shared; 5L7D the same. The z key is therefore almost always tied and x/y
   decide the order, lining the list up with the 3x3 tile grid.
2. Offset k inside a slab maps to tile (k//3, k%3) — k%3 is the y column.
3. 9UWI's real stride was 150. 150 mod 9 = 6, gcd(6,9) = 3, so the walk visits
   offsets 0, 6, 3, ... — all k%3 = 0, one y column. Varying slab sizes (9 and
   18) and the carved-out regions drift that phase slowly with z, which turns a
   fixed column into a monotone y drift.

Replaying the selection with the true strides reproduces both systems:

    9UWI stride 150 (mod 9 = 6, gcd 3)  replay rho=+0.489   observed rho=+0.618
    5L7D stride 172 (mod 9 = 1, gcd 1)  replay rho=+0.074   observed rho=+0.069

and moving 9UWI's stride by one kills it (148/149/151/152 give +0.03..-0.07),
while pushing 5L7D onto 171 (gcd 9) or 174 (gcd 3) raises it to +0.18. **5L7D
was not immune, it was lucky.** The within-slab offset histogram agrees: 9UWI
depletes offsets 1, 4, 7 (all k%3 = 1) about threefold, 5L7D is flat.

The strides I first assumed (166 / 182, from the full water set) do not
reproduce it — the real candidate list excludes non-bulk and near-protein
waters, giving 150 / 172. Reading the chosen ions' ranks in the sorted pool
settled it: 0, 150, 300, 450, ... exactly.

### Wrong answers along the way

Uncapped termini (true, but a localised +-1 cannot move both species one way),
protein net charge and the extended ICL3 (the candidate pool is uniform in y at
every carve cutoff, rho ~= 0), and a species-specific rule (both species drift
together). An earlier fix in this same function had already dealt with a
species-ordering bug; this one is in the site selection underneath it.

### Fix

One site per equal-sized block of the sorted list, position inside the block
drawn from a fixed seed (`ION_PLACEMENT_SEED`). Blocks keep the z spread the
stride was there for; a seeded draw cannot resonate with the tile period. Same
seed, same placement, so a rebuild still reproduces bit-for-bit. Measured on
the real systems: 9UWI rho_y +0.489 -> -0.033, 5L7D +0.074 -> +0.051, z range
preserved.

Tests assert the mechanism rather than a correlation magnitude: a resonant
stride never leaves its tile column (spread 0.0), the shipped selection always
visits every column, across ion counts 150..320.

### Impact

None on the runs already done — MD relaxes it. 9UWI upper <y>: +24.0 initial,
+2.5 after eq, +4.4 after prod, i.e. inside 1 sigma by the end of equilibration.

It would have mattered for replicates. The selection is deterministic, so the
same system rebuilt for a second replicate got the identical biased placement;
changing only the integrator seed would not have decorrelated the ions. That is
exactly the kind of hidden correlation adaptive sampling cannot afford.

---

## 2026-08-21 — SIF 名から cufft121-fusefix を落としていた（私のミス）

`a34dba5bdb21` の SIF を渡したところ、名前に `cufft121-fusefix` が無いことを指摘された。
**中身は入っており、名前だけの誤りだった。** SIF から実測:

```
MDCLAW_CUFFT_MIN_VERSION = 12.1.0.78
同梱 cuFFT               = libcufft.so.12.1.0.78 (API 12100)
MDCLAW_FUSEFIX_LIB       = /opt/mdclaw/lib/libmdclaw_fusefix.so
  LD_PRELOAD に載る = True / プロセスに mmap 済み = True
NVRTC 13.0 / math libs 13.1
```

### 何を間違えたか

あの系列のタグは積み上げだった (`cuda130` -> `cuda130-cufft121` ->
`cuda130-cufft121-fusefix`)。私は「今回の目玉は PPM3」と考えて末尾を `ppm3` に
**置き換えた**。しかし 3 つのタグはいずれも**ホスト互換性の契約** — NVRTC/PTX が
13.0、sm_100 の PME に必要な cuFFT の下限、FUSE マウント対策の preload shim —
であって、「このファイルがその環境で動くか」を決めるもの。PPM3 / MODELLER /
UTF-8 モードはソフトウェアの機能で、git revision が既に一意に特定している。

### 決めた命名規則

```
mdclaw-rikyu-arm64-<ホスト互換性の契約>-<git rev>.sif
現行: mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-<rev>.sif
```

**名前に載せるのは契約だけ。機能は revision に任せる。** そうしないと機能追加の
たびにタグが伸びる。契約が変わったとき (CUDA 世代を上げる、shim が不要になる)
にだけ名前を変える。

`~/Downloads` の SIF は改名済み (内容は同一、SHA-256
`e5b989e5b9bf9a4eff70c0185b39438e9bcccfd13a28714f5cc4765be34c275f`)。
過去エントリ中の `...-ppm3-<rev>.sif` という表記は同じ理由で誤り。

---

## 2026-08-20 — Prep chemistry and missing-residue contracts corrected after independent review

This review was done with a second agent and checked by direct measurement, not
only by reading the diff. No commit was made during the review.

The OpenMM bundled in `mdclaw.sif` was measured directly. Its `pdbNames.xml`
aliases ASH→ASP, GLH→GLU, CYX→CYS, and HID/HIE/HIP/HSD/HSE/HSP→HIS. LYN, CYM,
TYM, and ARN have no alias. An ALA-ASH-ALA PDB loads as ASP through both
`openmm.app.PDBFile` and PDBFixer and writes back as ASP. This corrects the
earlier 2026-08-20 memo entry's “second correction”, which said the loader was
not at fault, and the old `pdb_utils.py` docstring, which listed LYN→LYS and
CYM→CYS as loader normalizations. Both were wrong in the same direction.

Consequently, the old `from_input_structure` protonation label could never be
true for the aliased names: it described a state re-derived after the input
decision had already been erased. With `--missing-residue-method modeller` the
scan was also reading the MODELLER model rather than the user's input. That
machinery was removed. Prep now scans the original PDB or mmCIF with gemmi and
promotes raw-input ASH/GLH/LYN/CYM into caller overrides; explicit overrides
still win. These residues survive PDBFixer/PDBFile normalization, are reported
as `user_override` with `override_origin=input_structure`, and no longer move
when `--ph` changes. CYX remains under the disulfide bond contract, and
histidine tautomers remain separate.

The recovery contract introduced in 8980391 was measured to be unexecutable. A
prep node that fails with `pdbfixer_missing_residues_out_of_scope` is terminal
and sealed; re-running that node with `--missing-residue-method modeller`
returns `node_terminal`, although the recommendation said verbatim “Re-run
this same node with the flag”. Recovery now creates a new prep node with the
same completed parent, and the result names both commands: `create_node` with
the explicit parent, then `prepare_complex` on the new node with the MODELLER
method.

The deleted glycoprotein pipeline test exposed a separate pre-existing
structural error rather than a regression. 6YA2 chain C really has a
13-residue internal gap from 194 to 208; the flanking CA atoms are 15.49 Å
apart. Before 8980391, chain splitting dropped SEQRES, PDBFixer saw zero
reference sequences and reported no missing residues, and no residue was
modeled. `openmm.app.PDBFile.createStandardBonds` then joined SER194 C to
PRO208 N at 17.26 Å without a distance check, against a 1.33 Å equilibrium
peptide bond. The test therefore asserted success on a structure containing a
17 Å peptide bond. The corrected visibility rule has a real blast radius:
structures with more than 10 internal missing residues, or any single gap
longer than 5, now stop at prep where they previously passed silently.
`tests/test_pipeline_glycoprotein_dag.py` was deleted at the user's direction;
that also removed the repository's only GLYCAM topology integration coverage,
which remains a known test gap.

The review also found that the guardrail golden was stale for five codes and
two MODELLER conversion codes were not registered. MODELLER in-place repair
accepted any existing output, including a one-atom “successful” model.
`_restore_template_frame` discarded insertion codes, so template residues
100A/100B collided and the model kept MODELLER's 1..N numbering behind a
success result. Repair now requires complete target length and sequence,
preserved observed residue identities, and complete author renumbering;
insertion codes round-trip, while genuine numbering collisions fail.

On the topology side, Amber ASH/GLH/LYN/CYM restoration could skip silently
and still build a System at the default ASP/GLU/LYS/CYS charge, one elementary
charge wrong per missed residue. The restore now reports unique candidates,
and final topology validation checks both restore counts and the
variant-specific hydrogen identity (ASH HD2, GLH HE2, LYN without HZ3, CYM
without HG) under `amber_variant_restore_incomplete`.

Finally, `confirmation_needed` labelled mixed auto-detected protonation and
predicted MODELLER loops as `user_override`; its policy explicitly permits
skipping prompts for that source. Provenance is now derived per entry,
predicted coordinates use `source=predicted` with a separate
`method_requested`, and terminal-only omissions are reported as UNMODELED.
The pdb4amber+reduce fallback now reports protonation states at all. The
measured 9UWI case, where 77 terminal residues were left unmodeled, now reaches
the confirmation report instead of disappearing.

---

## 2026-08-20 — Missing residues were invisible inside prepare_complex; gaps are now repairable in place

Two problems, one fix. Started from the MODELLER ordering trap (fetching a
template completes the job's only `source` node, and a completed node is sealed,
so `modeller_from_alignment` cannot then write to it). Every CLI structure-
acquisition tool is `@node_tool("source")` — verified by running
`fetch_structure --output-dir` and getting `code=node_context_required` — so
inside one job there is no correct order to document. The workaround used for
9UWI left `studies/9uwi-popc` with three registered jobs (`main`, `modelled`,
`modelled2`) of which two hold nothing but a source node, and with `main` and
`modelled` recording `job_dir` against different path roots.

**The bigger finding: `prepare_complex` could not see missing residues at all.**
`split_molecules` builds each chain as a fresh `gemmi.Structure()` holding only
the modeled residues, so SEQRES stayed behind in the parent. PDBFixer finds
gaps by comparing coordinates to that reference; with none it reports zero.
Measured on 4AKE:

    source candidate (candidate_001.cif):  SEQRES chains=2   <- reference present
    split chain file (protein_1.pdb):      SEQRES chains=0   <- gone

So `pdbfixer_missing_residues_out_of_scope` could never fire from the
`prepare_complex` path, and internal gaps entered MD as silent chain breaks.
All three studies confirm it: `missing_residue_repair` was `None` on every prep
node of 4ake-apo-trial, 5l7d-popc and 9uwi-popc.

### What changed

`_carry_reference_sequence` (`mdclaw/structure/split.py`) copies the owning
entity's `full_sequence` onto the extracted chain. The recipe matters:
`setup_entities()` on the *new* structure first (gemmi writes SEQRES from an
entity whose subchains match the chain being written), then fill the empty
`full_sequence` it produced. Attaching the parent entity directly writes no
SEQRES at all — tried it, got 0 lines.

`missing_residue_method` on `clean_protein` and `prepare_complex`, default
`pdbfixer`, alternative `modeller`. With `modeller` the gaps are rebuilt before
PDBFixer runs, in the same prep node: the chain is its own template and its own
SEQRES is the target, so no template file, no target sequence, and no second
source node are involved. The DAG stays `source_001 -> prep_001 -> ...`, one
job, and `NodeSealedError` never appears.

Deliberately not done: no automatic escalation and no upper ceiling. Rebuilding
a 33-residue ICL3 is a scientific judgement, so it happens only behind an
explicit flag.

### Impact on existing studies, measured before shipping

    4ake  SEQRES=214  internal=0   -> unchanged
    5l7d  SEQRES=638  internal=0   -> unchanged
    9uwi  SEQRES=386  internal=40 in 3 segments, max 33, terminal 77
                                  -> now OUT_OF_SCOPE (was silent)

One of three studies changes behaviour, and it is the one that actually needed
MODELLER. 5L7D turned out to have no internal gaps — worth recording, since I
had assumed a cryo-EM GPCR would.

### Verified end to end on 9UWI chain A

Default path stops with the code unchanged (`pdbfixer_missing_residues_out_of_scope`
is a public contract) but the first recommended option is now
`repair_in_place_with_modeller` carrying `--missing-residue-method modeller`,
where it used to say "regenerate the source" and point straight at the two-job
trap. MODELLER path: 40 residues rebuilt in 3 segments, longest 33, seed -8123.

Residue numbering survives: all 269 observed residues keep both number and name,
model 309 residues total. The first attempt produced 386 — the whole SEQRES,
including a fabricated 67-residue C-terminal tail — because the full reference
sequence was handed to MODELLER. Fixed by modeling only the span between the
first and last observed residue, so unresolved termini are left alone exactly as
the terminal filter intended.

One bug found by running it end to end rather than at unit level: detection
originally ran on the post-repair file, which is a MODELLER model with no
SEQRES, so the node reported `status="not_detectable"` for chain A directly
under a line saying 40 residues had just been rebuilt. Detection now describes
the structure as it was before repair (SEQRES 386, modeled 269, terminal
excluded 77).

### Also now reported

A chain with no reference sequence reports `status="not_detectable"` rather than
zero gaps — "not checked" and "none present" were indistinguishable before.
Terminal segments excluded from repair are reported with segment *and* residue
counts (9UWI: 2 segments, 77 residues); the old message counted segments while
saying "residue(s)". The MODELLER random seed and the template's sha256 go on
the node, because a loop that cannot be reproduced makes the whole study
irreproducible. In a multi-chain structure each repair is tagged
`interface_context: chain_isolated`, since chains are repaired from separate
files and an interface loop never sees its partner.

`tests/test_missing_residue_handling.py` builds its inputs with gemmi rather
than downloading them, so it runs on a compute node with no network. The
two-chain fixture uses deliberately different sequences and lengths per chain: a
chain-to-entity mix-up survives a same-length check, and 4AKE and 5L7D both have
identical chains, so neither would catch it.

Not in the shared SIF. Members run the container's own mdclaw.

---

## 2026-08-20 — MDDataBench を別リポジトリに切り出した

MDPrepBench / MDStudyBench と同じ形で `/home/yasu/tmp/MDDataBench` に独立させた。
mdclaw 側の `benchmarks/mddatabench/` と `docs/research/db_derived_benchmark_validation.md` は削除済み
(どちらも git 未追跡だったので履歴操作は不要)。**本エントリより前の 8/18-8/19 の各エントリが参照している
`docs/research/db_derived_benchmark_validation.md` は、いまは `MDDataBench/docs/validation-design.md` にある。**
過去エントリは規約どおり書き換えていないので、参照を辿るときはここを見ること。

**構成は MDPrepBench に合わせた。** hatchling + `mddatabench` コンソールスクリプト、
`mddatabench.TOOLS` を signature 由来のフラグでディスパッチする `__main__.py`、
`benchmarks/mddatabench/tasks/`、`tests/`、`.github/workflows/ci.yml`、MIT LICENSE、
CLAUDE.md と AGENTS.md の同一二枚。スクリプト群はパッケージモジュールに移した
(`subspace_test.py` -> `subspace.py`、`execution_check.py` -> `execution.py`、
`fetch_reference.py` -> `reference.py`、`score_submission.py` -> `scoring.py`、
`negative_controls.py` -> `controls.py`)。argparse の `main()` は全部ライブラリ関数に直して
`cli.py` の TOOLS から呼ぶ形にした。

**CLI は 4 つ**: `list_benchmark_tasks` / `fetch_benchmark_reference` /
`score_benchmark_submission` / `run_benchmark_negative_controls`。

**動作確認**: ruff clean、fast テスト 14 本 passed (0.62 s)、
`mddatabench score_benchmark_submission` で D01 が **prep 7/7 md 5/5 = 12/12 を 6.2 秒**。

**テストに入れた不変条件**: ライセンスが CC 系であること、bundle の SHA-256 が 3 ファイル分揃っていること、
全チェックが `prep`/`md` のどちらかに分類され `check_type` が `@1` 付きであること、md 側が
構造のみ帰無検定と時計の両方を持つこと、そして **prompt が accession / MDDB / MoDEL / DOI を漏らさず、
かつ PDB ID と採点対象の条件 (水モデル・温度・アンサンブル) は述べていること、`rmsip` を含まないこと**。
最後のはプロンプト最小化とリーク防止を機械的に守らせるためのもの。

初期コミット 29 ファイル / 328 KB、データは 0 バイト。GitHub remote は未作成 (ユーザ判断待ち)。

---

## 2026-08-20 — MODELLER now converts an mmCIF template instead of renaming it

Fixes trap 2 from the 9UWI entry below. `modeller_from_alignment` staged the
template into MODELLER's working directory with `shutil.copy2(template_path,
out_dir / f"{code}.pdb")` — extension change only. MODELLER picks its reader
from the file's contents, not its name, so an mmCIF under a `.pdb` name is not
degraded, it is unreadable: `read_pd_702E> ... file is probably corrupt` on the
first CIF line.

New `_stage_template_as_pdb` copies a PDB source and converts an mmCIF one via
gemmi (`make_structure_from_block` → `setup_entities` → `write_pdb`). The
conversion is reported as a warning, because PDB cannot hold everything mmCIF
can — residue names longer than three characters, more chains than single
letters. Failures return structured codes rather than a corrupt file:
`modeller_template_conversion_unavailable` (no gemmi) and
`modeller_template_conversion_failed` (unparsable input).

Verified against the file that produced the original failure: the full
`9UWI.cif` (562 residues, Atosiban included) passed straight to `--template-pdb`
with the 269-residue chain-A sequence now builds a model —
`selection_reason: lowest_dope_score`, DOPE -37149, CA RMSD after fit 0.748 Å —
where before it died in MODELLER's PDB parser. The staged `9UWI.pdb` contains
no `_atom_site.` or `loop_` lines. `tests/test_modeller_template_staging.py`
covers copy, conversion, the `.mmcif` suffix, and the unparsable case; 204 tests
and ruff pass.

The chain-A workaround written by hand for that run
(`studies/9uwi-popc/templates/9UWI_A.pdb`) is no longer needed for format
reasons. It is still the right input when the template should exclude the other
chains and the ligand — the tool converts the file it is given, it does not
subset it.

Not in the shared SIF. Members run the container's own mdclaw, so this and the
other fixes from 2026-08-19/20 reach them only on the next image rebuild.

---

## 2026-08-20 — 9UWI chain A through MODELLER into POPC; three traps on the way

Second member target, and the first real exercise of the MODELLER path baked
into the SIF: 9UWI (human V1a receptor, cryo-EM 2.8 A), chain A only, Atosiban
and the cholesterols dropped, the three internal gaps rebuilt, POPC bilayer,
1 ns production. It works, and the run found three things worth fixing.

**The gaps.** Chain A is observed 43-351 (269 residues) against a 386-residue
SEQRES, with internal gaps 80-84 (3), 157-162 (4) and 247-281 (**33**, ICL3).
Author numbering is offset -32 from SEQRES, so the target sequence has to be
built by aligning observed residues rather than slicing SEQRES by author number
(269/269 match at that offset). The 33-residue gap is above the default
`--loop-max-length 30`, so it needs raising or ICL3 stays unrefined — the skill
does warn about this. Target span 43-351 only: modelling the unobserved 1-42 and
352-386 would invent 77 residues of terminus flapping in solvent.

Result: 4 loop models, best by DOPE (-3226, molpdf 213), 309 residues, **no gaps
left**. `--template-frame` reported `ca_rmsd_in_place: 13.552` / `after_fit:
0.823`, but measuring it directly over the 269 common CA gives mean deviation
0.28 A and a centroid shift of 3.5 A — the model *is* in the template frame.
Whatever the reported 13.55 is measuring, it is not the in-place deviation an
agent would read it as. Worth a look.

**Trap 1: the skill never says when to run MODELLER relative to
`fetch_structure`.** Node mode writes into the source bundle, so the source node
must still be open — but `fetch_structure` completes it, and loop refinement
needs a template structure, which is exactly what you would use `fetch_structure`
to get. Running MODELLER after it gives `NodeSealedError`.
`skills/modeller-predict/` does not mention `fetch_structure` anywhere. Worked
around by fetching in one job and running MODELLER on a fresh source node in a
second job registered with `add_study_job`.

**Trap 2: `--template-pdb` accepts an mmCIF and does not convert it.** The file
is copied to `<code>.pdb` and handed to MODELLER as-is, which then fails with
`read_pd_702E> ... file is probably corrupt` at the first CIF line it cannot
parse as PDB. Converting to a real PDB first fixed it. Restricting the template
to chain A also dropped Atosiban's `A1EQM`, whose 5-character residue name has
no PDB representation.

**Trap 3: a protonated aspartate renamed lipids, and nothing said it was
there.** Two aspartates (97, 112) came back protonated, but
`confirmation_needed.protonation_states` was `{"source": "auto_detected",
"states": []}` — empty. `embed_in_membrane` then failed at the net-charge step:

    No template found for residue 399 (ASH). The set of atoms matches PA, but
    the residue has no bonds between its atoms.

`--protonation-states '{"A:97": "ASP", "A:112": "ASP"}'` gets past it. 5L7D never
hit this, so it is structure-dependent — any member whose receptor has a buried
Asp will.

**First correction: ff19SB does have an ASH template**, so "no template found"
is not about the force field lacking one. OpenMM matches templates by atom
composition, not by name.

**Second correction — the loader was not at fault either.** `pdbNames.xml`
registers `ASH` as an alias of `ASP` (and `HID`/`HIE`/`HIP` of `HIS`), so
`openmm.app.PDBFile` normalises it and bonds fine; measured on a synthetic
ALA-ASH-ALA, `ASH` loads as `ASP` with 14 bonds whether written as ATOM or
HETATM. `LYN` and `CYM` have no alias and are the ones that would load bare.
So a residue arriving at template matching still *named* `ASH` never went
through that normalisation — which points at MDClaw's own code.

**The actual cause is in `mdclaw/amber/openmm_build.py`.** The topology path
deliberately rewrites ASH/GLH/LYN/CYM to their CCD names so Pablo can identify
them, then restores the Amber names on the loaded topology. The histidine
restore guards on `residue.name != "HIS"`; the variant restore had no such
guard and renamed on `(chain, residue number)` alone. **That key is not unique
in an assembled membrane.** In this system chain A carries both the protein
`ASP` 97/112 and POPC `PA` residues numbered 97 and 112, so lipid tails were
renamed to `ASH`. The force field's "no template for residue 399 (ASH), the set
of atoms matches PA" was reporting precisely that: a PA residue wearing the
name ASH.

Two defects, then, and neither is the force field or OpenMM:

1. **Restore.** Fixed: `_restore_amber_variant_names` now checks the recorded
   chain and that the residue still carries the base name that was substituted,
   before renaming it back. Extracted to a module-level helper with tests in
   `tests/test_amber_variant_restore.py` covering the lipid, water and ion
   collisions and all four variants. Verified on the failing system: the same
   prep that produced `membrane_neutralization_failed` now embeds cleanly with
   both aspartates kept as ASH and the lipids untouched.
2. **Reporting.** `confirmation_needed.protonation_states` only ever carried
   caller-supplied overrides, never what pdb2pqr assigned — while
   `histidine_states` reads the produced structure. Fixed: added
   `_extract_non_default_protonation_states` and `_merge_protonation_states`,
   wired into all three recording sites in `clean_protein.py` and into the
   operation records the summary aggregates from. Reported names are kept in
   step with what the topology path can round-trip (ASH/GLH/LYN/CYM; ff19SB has
   no TYM or ARN template, so promising them would promise an unbuildable
   system). The failing 9UWI prep now reports both aspartates with the state
   they replaced.

The workaround used during the run — forcing the aspartates back to ASP — was
therefore treating a symptom. It is no longer needed.

**The run.** Orientation came from OPM homolog **7QVM** (identity 0.60, fit_rmsd
2.29 A, hydrophobic thickness 31.8 A), not from 9UWI itself — so unlike 5L7D
this exercised the homolog transfer rather than an exact self-match. Box
116 x 77 x 104 A, rectangular in the membrane plane rather than square, but the
receptor keeps ~18 A of lipid to its periodic image against the 15 A requested.
`min` 21 s, `eq` (1.5 ns) 3 m 48 s, `prod` (1 ns) 2 m 51 s. Production held
300.99 +/- 1.03 K and 1.032 +/- 0.002 g/mL over 100 frames.

Final DAG: 8 completed, 1 failed (`solv_001`, the ASH failure, kept as evidence).

---

## 2026-08-20 — 5L7D in POPC end to end on Rikyu; the membrane fixes hold, and one analyze trap

Ran the v0.6.8 SIF through a real membrane system to check the fixes before
telling the group to pull: 5L7D (human Smoothened, a class F GPCR with a BRIL
fusion), chain A only, ligand CLR and the NAG glycans dropped, POPC bilayer,
0.5 ns NVT + 1.0 ns NPT, 1 ns production. Nine nodes, **0 failed**.

**The membrane fixes hold.**

| Check | Result |
| --- | --- |
| Orientation backend | `opm-homolog`. 5L7D is itself in OPM, so identity 1.0, `fit_rmsd` 0.0, hydrophobic thickness 32 A, 10 candidates evaluated. No PPM3 fallback. |
| Ions in the bilayer | 301 ions, **0** within the middle 80 % of the lipid z-span. This is the `a6cad27` fix (patch salt no longer carried into the assembly) working on a real system. |
| Lipid headgroup restraint | `lipid_headgroup_restraint_count: 359` on both `min` and `eq` — the new flat-bottom restraint is applied. |
| Box fitted to solute | 116 x 116 x 173.5 A. The extracellular CRD sets the z height; nothing crosses the cell after NPT contracted it 10 %. |
| Equilibration | Density 0.917 -> 1.024 g/mL, volume 2416 -> 2164 nm3. |
| Production | 300.34 +/- 0.68 K, 1.025 +/- 0.001 g/mL, backbone RMSD rising to ~0.15 nm and flat after 40 frames. |

**Throughput:** ~300,000 atoms, one GB200. `min` 32 s, `eq` (1.5 ns) 7 m 58 s,
`prod` (1 ns) 6 m 09 s — about **270 ns/day**. The whole run cost ~15 min of GPU.

**The trap: `explain_node` says an analyze node is ready when its metric tool is
not.** Create an `analyze` node parented on `prod`, and `explain_node` reports
`ready_to_run: true`, no blocking codes, no missing inputs, and resolves
`topology_file` / `trajectory_chain` / `energy_chain`. Running `analyze_rmsd` on
that node then fails:

    Validation failed for 'trajectory_file / reference_pdb': Both are required.

The resolver exposes `trajectory_chain` (a list) and `topology_file`; the metric
wants `trajectory_file` and `reference_pdb`, which only exist after
`concat_trajectory` has run on that node and written `combined_trajectory` +
`reference_pdb`. `skills/md-analyze/metrics.md` states this ("After
`concat_trajectory` ... the combined trajectory and reference PDB are the common
inputs"), so an agent that reads the skill is fine. An agent that trusts
`explain_node` — which is what `run-loop.md` says to check before running a
stage tool — is not. Reproduced cleanly on a fresh node (`analyze_003`).

Either `explain_node` should report the concat prerequisite for metric-bearing
analyze nodes, or the metrics should accept the chain form the resolver already
hands them. Not fixed here.

**Not a finding, for the record:** `analyze_rmsd` does return its statistics —
`mean_rmsd_nm`, `std_rmsd_nm`, `max_rmsd_nm`, `n_frames` as flat keys. An
earlier read of this session looked for a `statistics` object that the tool
never promised.

---

## 2026-08-20 — UTF-8 モードを焼いた SIF (acabf7612b72)

locale 修正 (`preserve_locale` / `new_simulation`)、`bin/mdclaw` の bash 3.2 対応、
両イメージへの `PYTHONUTF8=1` を含めて焼き直した。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-acabf7612b72
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-acabf7612b72.sif
        6,774,358,016 bytes
        SHA-256 acbc89e6e279376b2fa8d1be6f8019a6e0b609c10c83c7e4b71cb83c88f1522d
smoke   Docker 24/24、SIF からも 24/24 (24 件目が UTF-8 mode contract)
```

SIF から実地確認した 2 点:

```
read-only CWD で mdclaw --version   -> mdclaw 0.6.8、outputs/ も残らない
LANG=C.UTF-8 + LC_ALL=C 後の書き込み -> utf8_mode=1、em dash が往復する
```

後者は `PYTHONUTF8=1` が無ければ `UnicodeEncodeError` になる条件。これで
「ドライバがロケールを倒す」経路は、MDClaw 内 (preserve_locale) と第三者ライブラリ内
(UTF-8 モード) の両方が塞がった。

前世代の SIF: `36f13d131a89` は locale 修正前、`34230ff20567` は CWD 修正が 1 ファイル
欠けた失敗作。どちらも破棄してよい。

---

## 2026-08-20 — フルスイートの 21 failure の原因は OpenCL ドライバの setlocale だった

「単体で走らせると通り、フル実行だと落ちる」21 件。**flaky ではなく実バグ**で、原因は
1 つだった。

### 特定までの経路

`read_text()` が ASCII デコーダで落ちていたので locale を疑ったが、環境変数をどう変えても
Python 3.12 は C ロケールを UTF-8 へ強制するため再現しない。テストモジュールの import を
1 つずつ追っても倒れない。**全ファイル収集 + 1 テストだけ実行では再現せず、実行すると再現**
したので「どれかのテストの実行中」に絞り、5 ファイルまで縮めて 22 秒で再現、二分探索で
`test_ap5_build_topology_smoke.py::test_step4_topology_via_gaff` に到達した。

Python の `locale.setlocale` / `ctypes.CDLL` / `subprocess.Popen` を全部張っても検出できず
(C レベルの dlopen 経由だったため)、**2 ms 周期で LC_CTYPE を読みメインスレッドのスタックを
ダンプするサンプラースレッド**で瞬間を捕まえた。

```
build_amber_system -> openmm_build.py:1555 Simulation(...) -> mm.Context(...)
-> プラットフォーム未指定なので OpenCL が選ばれる
-> Apple の OpenCL ドライバ初期化が setlocale(LC_ALL, "C")
-> プロセスの既定エンコーディングが US-ASCII に固定
-> 以降 encoding 未指定のテキスト I/O が全部 ASCII、em dash (U+2014) で死ぬ
```

### テストだけの問題ではない

`render_structure_preview` の 4 件は **MDClaw が自分の出力を書けずに** `UnicodeEncodeError`
で落ちていた。Linux arm64 イメージ内で実測:

| 環境 | `utf8_mode` | ドライバが LC_ALL=C にした後 | 結果 |
|---|---|---|---|
| LANG 未設定 (コンテナ既定) | 1 | utf-8 のまま | 影響なし |
| LANG=en_US.UTF-8 | 1 | utf-8 のまま | 影響なし |
| **LANG=C.UTF-8** | **0** | **ANSI_X3.4-1968** | **UnicodeEncodeError** |

`LANG=C.UTF-8` は HPC で普通に設定される。**macOS 固有ではない。**

### 修正

`_common.py` に `preserve_locale()` と `new_simulation()` を追加し、MDClaw が Simulation を
作る 12 箇所すべてを経由させた。ドライバは今も倒すが、プロセスには残らない。`bin/mdclaw` の
bash 3.2 問題 (`set -u` 下の空配列展開) も別件として直した。

`tests/test_locale_guard.py` は復元・例外時の復元に加えて、**`_common.py` 以外で `Simulation(`
を直接呼んでいないこと**をソースレベルで検査する (ガードは全呼び出し箇所が使って初めて意味が
あるため)。1 箇所戻すと落ちることを確認済み。

```
before  21 failed, 1454 passed
after   1478 passed, 7 skipped, 0 failed
```

`PYTHONUTF8=1` をイメージに入れれば表の 3 行目も構造的に消えるが、未実施。

---

## 2026-08-20 — CLI が read-only CWD で起動しない件を修正。SIF 再作成。既存の 21 failure は locale 依存

### 修正した本体

`WORKING_DIR` を宣言する 28 モジュールが、その隣で **import 時に** `ensure_directory(WORKING_DIR)`
を呼んでいた。`_cli._discover_tools()` は全モジュールを import するので、**何もしない
`--version` / `--list` ですら CWD に `outputs/` を掘り、掘れなければ CLI 自体が起動しない**。
書き込み側は全箇所すでに `ensure_directory` / `create_unique_subdir` / `mkdir(parents=True)`
を呼んでいるので、import 時の呼び出しは最初から不要だった。28 件すべて削除し、不要になった
import も除去。回帰テストを `TestSubprocessCLI` に 2 本追加した。

### 自分のミスを 1 件記録しておく

回帰テストに効き目があるか確かめるため `pdb_client.py` にバグを一時的に戻し、`git checkout`
で戻した。**これはインデックスから復元するので、実験で足した行だけでなく修正ごと巻き戻した。**
結果 27/28 だけ直った状態でコミットし、その状態で SIF を焼いて渡した。さらに、フルスイートで
自分の回帰テスト 2 本が落ちているのを「テスト中に git を触ったせいの flaky」と誤診した。
**テストは正しく、読み違えたのは私。** 教訓は 2 つ: 実験の巻き戻しは `git checkout <file>` では
なく `git stash` / `git diff > patch` を使う。落ちたテストを flaky と判定するなら、根拠は
「再実行して通った」ではなく原因の特定。

### 既存の 21 failure は私の変更と無関係、原因は locale 依存の `read_text()`

修正前のコミット `2daf6e4` のワークツリーで同じフルスイートを流し、**失敗集合が完全に一致**
することを確認した (pre-fix 21 failed / 1452 passed、post-fix 21 failed / 1454 passed、
差は追加した回帰テスト 2 本)。

原因は、リポジトリ内のファイルを `read_text()` で **`encoding=` を渡さずに**読んでいること。

```
claude = (REPO_ROOT / "CLAUDE.md").read_text()
  -> encodings.ascii.IncrementalDecoder ... UnicodeDecodeError
```

プロセスのその時点の locale に依存するので、単体実行では通り、フル実行では途中で locale が
C に倒れて日本語を含むファイルで落ちる。**C locale が既定のコンテナ / HPC batch では常に
落ちる**性質のもので、flaky ではない。未修正。

### 別件: `bin/mdclaw` は bash 3.2 で動かない

`tests/test_bin_wrapper.py::test_wrapper_is_quiet_outside_a_user_namespace`:

```
bin/mdclaw: line 146: NV[@]: unbound variable
```

`set -u` 下の空配列展開 `"${NV[@]}"` は bash 4.4+ では通るが bash 3.2 (macOS 既定) では
エラー。`"${NV[@]+"${NV[@]}"}"` で直る。未修正。

### 成果物

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-36f13d131a89
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-36f13d131a89.sif
        6,774,267,904 bytes
        SHA-256 0324302a3943324f5bc73bda548bf92eef74cd247f1346b69d2a52546298fd9d
smoke   Docker 23/23、SIF からも 23/23
```

read-only CWD からの起動を実地で確認した (SIF 内、0555 のディレクトリを `--pwd` にして
`mdclaw --version` → `mdclaw 0.6.8`、ディレクトリには何も残らない)。**1 つ前の SIF
`...-34230ff20567.sif` は修正が 1 ファイル欠けた版なので使わないこと** (同じ条件で
`OSError: Read-only file system: 'outputs'` を再現する)。

---

## 2026-08-20 — v0.6.8 の arm64 SIF (a6cad2701ac4)。CLI が read-only CWD で起動できない件を発見

`3997c36..a6cad27` の 8 コミット (patch の塩を assembly へ持ち込まない、water copier が
slab を薄くする問題、solute と接する分子を一緒に image する、99,999 原子超の preview、
box の書き出し、v0.6.7 リリース) を取り込んで焼き直した。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-a6cad2701ac4  (mdclaw 0.6.8)
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-a6cad2701ac4.sif
        6,774,267,904 bytes
        SHA-256 098df878a4593ecbd1a0c68e89d662402b566ac567e1182632fd0779adff8d72
smoke   Docker 23/23、SIF からも 23/23 (GPU は SKIP)
```

前回同様 GHCR には push せず、`docker save` した tar を Lima VM に読ませて変換した。

### `mdclaw --version` が読み取り専用の CWD で落ちる

SIF 検証のついでに read-only なディレクトリから `mdclaw --version` を叩いたら死んだ。

```
File "mdclaw/research/pdb_client.py", line 22, in <module>
    ensure_directory(WORKING_DIR)
OSError: [Errno 30] Read-only file system: 'outputs'
```

`WORKING_DIR = Path("outputs")` が 20 以上のモジュールにハードコードされていて、どれも
**import 時に** `ensure_directory(WORKING_DIR)` を呼ぶ。`_cli._discover_tools()` は全
モジュールを import するので、**`--version` や `--list` ですら CWD に `outputs/` を掘る**。
掘れなければ CLI 自体が起動しない。

`test-container.sh` が緑なのは、冒頭で `cd "${TMPDIR:-/tmp}"` して書ける場所へ移るから。
つまり既存のスモークではこの経路を踏まない。HPC で read-only bind や書き込み権限の無い
ディレクトリから叩くと、ツールを1つも実行しないうちに落ちる。副作用として、無関係な
ディレクトリで CLI を触るだけで空の `outputs/` が生える。

未修正。import 時の副作用を消し、必要になった時点で作るのが筋 (WORKING_DIR の既定を
`.` にする件とも絡む)。

---

## 2026-08-20 — パッチの塩を持ち込むのをやめた。イオン中和の分岐は全部これが原因だった

前日の膜構築修正のレビューで `_drop_counter_ions` の欠陥を指摘され、直す前に
パッチのイオンを実測したところ、前提が崩れた。

### 測定

バンドルされた 7 パッチはいずれも **イオン 8 個 (Na 4 / Cl 4)** しか持たない。
各パッチの midplane を `_estimate_patch_membrane_center_z` で取り、z を最小イメージで
測ると:

```
POPC            core(|dz|<15): 0   headgroup(15-25): 6   bulk(>25): 2
DOPC / DPPC                  : 0                    : 3            : 5
POPE / POPC:CHL1             : 0                    : 0            : 8
```

**疎水コアにイオンは 1 個も無い。** POPC パッチの脂質テールは |dz| <= 19.1 まで、
頭部基 PC は |dz| 10.2..27.9。イオン 8 個は dz = -26.95, -27.19, +23.55, +18.47 (Na)
と -15.67, -20.98, +20.00, +19.13 (Cl)。

削除条件は `|z - centre| <= leaflet` の leaflet = 23 なので、8 個中 5 個 (Na 1 / Cl 4)
を消す。9 タイルで 45 個、種の差 27 個 —— 実行時の warning
「dropped 45 ion(s) ... and 27 counter-ion(s)」と完全に一致する。コードのコメントは
"ions ... end up in the hydrophobic core of every copy" と書いていたが、**コアではなく
頭部基界面**だった。

### 現状のコードは既に事実上「全剥がし」だった

45 + 27 = 72 = 8 x 9 で、2 段階の削除がパッチのイオンを全部消していた。
`existing_cations: 0, existing_anions: 0` で中和が全量 (149 Na / 152 Cl) を配置。
設計ではなく、このパッチの組成による算術上の偶然。

### 決めたこと: パッチは塩ありで平衡化し、使う前に剥がす

VMD (`membrane` プラグインのパッチはイオン無し + `autoionize` が水を置換) と
CHARMM-GUI (二重層と水を組んでから水相にイオン配置) と同じ流れ。MDClaw の
`_apply_neutralizing_swap` も既に leaflet の外の bulk 水と交換しているので、
最終配置は元から同じ方式だった。パッチが塩を持ち込むことだけが違っていた。

消えた分岐:

| 消えた処理 | 理由 |
|---|---|
| 埋没イオン削除 | パッチにイオンが無い |
| `_drop_counter_ions` | 種の偏りが発生しない |
| `pairs_present` 分岐 | `existing = 0` が不変条件 |
| 超過分岐 | 到達不能 |

超過分岐は実害があった。`plan_neutralizing_ions` の docstring が
"never a negative number — ions that are already there are not removed to hit a
target from above" と明言しており、実測で 150 pairs present / 87 target なら
0.258 M、300 pairs なら 0.515 M が **警告無しで出荷される**。剥がせば到達不能になる。

実系 (SMO 5L7D / POPC) で検証: existing 0/0、149 Na / 152 Cl、net +3、
water 55,026、塩 0.1503 M、geometry passed、密度 0.957 of bulk —— 剥がし前と同一。

### レビューとの相違

cursor は全剥がしに反対し、「界面の平衡化イオンを残し、超過分を対で除去せよ」と
主張した。理由は「MDClaw の最終配置は決定論的な bulk 水置換であって CHARMM-GUI の
静電考慮 MC ではないので、平衡化済みの界面分布を捨てるのは改悪」。

採らなかった理由: 保持されるのは **8 座標を 9 タイル分そのまま複製したもの**で、
平衡化が生んだ分布ではなく人工的な周期性。最終系のイオン 301 個のうち 72 個が
それになる。タイル化由来の人工周期性は別途記録済みの既知問題でもある。

ただし **荷電脂質では cursor の主張が効く**。POPG/POPS/POPA では対イオンが頭部基に
凝集し、量も多く、それは本物の化学。荷電脂質のパッチを実際に作る際は、この判断を
再検討すること。現時点でそのパッチは存在せず、検証もできない。

### 同時に直したもの (レビュー指摘)

- imaging の接触判定を分子重心から**最近接原子**へ。screen も分子サイズ基準に。
  cursor の再現ケース (20 nm 箱、anchor に 0.2 nm で接触しつつ 12 nm 伸びる鎖) が
  3.0 nm -> 0.0 nm
- 密度ゲートに **xy 列判定** (10 A 角、bulk の 0.35 未満、z 方向に 80% 以上貫通)。
  有効範囲は両側から挟まれる: 断面 25% のチャネルはスラブ判定が 0.729 で既に落とし、
  8% 未満 (112 A 箱で直径約 35 A) はどちらも通さない。担当するのはその間
- 密度が評価不能なとき warning を出す (従来は passed のまま黙って通っていた)
- preview の `connect_mode 3` を hybrid-36 が実在するファイルだけに限定
- 大規模系 (>= 150k atoms) の system_box は影と antialias を落とす。
  **264 s -> 65 s** (timeout 300 s)。`ray=False` はヘッドレスでは PNG が出ないので不可

### 生成 PyMOL スクリプトの構文検証を追加

`json.dumps(True)` は JSON の `true` を返すので、生成 Python に埋めると構文エラーになる。
このセッションで 2 回踏んだ (`axis_views`, `fast_ray`)。2 回目は `connect_mode` の
条件分岐を**丸ごと無効化**していて、レンダリングが 3 秒で失敗して初めて気付いた。
フラグの全組み合わせ 24 通りを `ast.parse` で検証するテストを追加。


## 2026-08-19 — 検定を ANM 帰無に一本化、Rg の役割が判明、3 レプリカを測定、SIF の BLAS バグを発見

**検定を 1 本に絞った (ユーザ指示「H0 か ANM かに絞ったほうがいい」).** ANM 帰無 (cutoff 7.0-20.0 A の 27 点、
平均 0.517 SD 0.048 最大 0.588) はランダム帰無を包含する: 0.13 のものは 0.59 を超えられない。
実測 z は 本物の 1 ns +4.37 / 100 ps +2.26 / 10 ps -2.00 / ANM ensemble -0.05 (帰無のど真ん中) /
等方ノイズ -8.00。ランダム帰無はゲートから外し報告用の文脈に降格。負の対照 5 本すべて失敗、実 run のみ通過。

**Rg は冗長ではなかった。** 「Rg は意味なくない?」を実測で確かめたところ逆の結論になった。
**RMSIP はスケール不変**なので、軌道を一様に 1.3 倍しても RMSIP は 0.729 のまま変わらず (Rg は 1.14 -> 1.48)。
0.8 倍でも同様。進行性膨潤 (Rg 1.82) でも RMSIP は 0.711 でまだ通る。**Rg が振幅・コンパクトさを拘束する
唯一のチェック**であり、単位ミスや変性を捕まえる役割を独占している。バンドが緩い (結晶構造が満たす) のは事実だが
冗長とは別問題。

**3 レプリカの効果を実測 (seed 20260820/21 で 2 本追加).** 単独 0.729/0.743/0.717 -> 平均 0.730 **SD 0.010**、
レプリカ間 0.780/0.851/0.770 平均 0.801、3 本プール 0.764 (+0.034)。帰無最大からの余裕は +0.142 -> +0.176。
敵対側 (ANM ensemble 3 本) もプールで +0.022 稼ぐので、**利得は実在するが劇的ではない**。
本当に新しいのは**参照を使わないレプリカ間一致**で、これは `execution_validity` 軸に入る。
実務的含意: **SD 0.010 なのでエージェント間の 0.03 未満の差はノイズ。** レプリカ無しではこれが分からない。

**SIF の OpenBLAS がスレッド過剰生成で崩壊していた (プロジェクト全体に効く).**
scorer が 1 タスク 10 分超かかるので profile したところ `anm_null_distribution` が 528 秒。
Hessian 構築をベクトル化しても改善せず (結果は RMSIP=1.000000 で完全一致)、犯人は `np.linalg.eigh` だった。

| 環境 | `eigh(684x684)` |
|---|---|
| SIF、スレッド env 無し | **16.34 s** |
| SIF、`OMP_NUM_THREADS=1` | 0.12 s |
| SIF、`OMP_NUM_THREADS=8` | 0.07 s |
| ホスト python3 | 0.59 s |

`matmul` は 3.7 倍差なので LAPACK 固有。SIF の numpy は scipy-openblas を
`DYNAMIC_ARCH NO_AFFINITY MAX_THREADS=64` で積んでおり、32 コア機で上限未設定だと小問題が崩壊する。
**SIF 内で numpy を回すときは常に `OMP_NUM_THREADS` を渡すこと。**
`scripts/_threads.py` で `os.environ.setdefault` により numpy import 前に防御 (1 タスク 10 分超 -> 7.4 秒)。
恒久対応はコンテナか `bin/mdclaw` 側だが未着手。memory にも記録。

**最終スコア**: D01 prep 7/7 md 5/5 (12/12)、D02 prep 8/8 md 5/5 (13/13)、両タスク合計 13.5 秒。

---

## 2026-08-19 — 膜系の水が bulk の 40 % しか入っていなかった。コピーが自分自身と衝突判定されていた

PyMOL の緑箱が平衡化後の値かという問いから始まって、成果物側の 2 件と、
その図で初めて見えた本体側の 1 件を直した。

### 1. min/eq/prod の PDB は build 時の箱を書いていた

`PDBFile.writeFile` は CRYST1 を **topology** から取る。run 側の Topology は
`topology.pdb` を読んだときのままなので、NPT で箱が変わっても更新されない。実測:

| | box (A) |
|---|---|
| `equilibrated.pdb` の CRYST1 | 118.088 x 118.088 x 175.733 |
| `equilibrated.xml` (実際) | **104.894 x 104.894 x 164.713** |

各辺 11 %、体積 1.4 倍の過大表示。min/eq/prod の 3 箇所とも。同じリポジトリの
`export_state_to_pdb` (`platform.py:113`) は state から箱をコピーしていて、
patch 平衡化経路だけが正しかった。

### 2. `center_solute_and_wrap_solvent` は build 時にしか呼ばれていなかった

関数は正しい。呼ばれるのが `openmm_build.py:1588` と `openmm_system/build.py:607`
だけで、min/eq/prod は生の積分座標をそのまま書いていた。実測で水は x に 225 A
(箱 104.9 A の 2.2 倍) まで広がっていた。溶質は無傷。

どちらも `render_simulation_pdb_preserving_resnames` に `box_vectors` / `image` を
足して修正。**state.xml / .chk / DCD には触らない**ので MD の結果は不変。

### 3. `extend_water_slabs` が追加した水は bulk の 40 % だった

修正後の図で上端の空隙が見えたので測ったところ:

```
z -45..-30 (パッチ自身の水)              89-96 % of bulk   <- 正常
z  30..95  (extend_water_slabs が追加)   35-43 %
```

**原因: 受理した分子の原子を、判定に使っている grid にその場で追加していた。**
コピー 1 つの中で先に置いた水と後の水が互いに衝突判定される。水は水素結合して
いるので H...O = 1.8 A < cutoff 2.2 A (全原子判定)。**正しい水の構造そのものが
「重なり」と判定され**、「互いに 2.2 A 以上離れた部分集合」まで間引かれる。
それが bulk の約 40 %。

3 通りで確認した。計装した実行の per-copy 生存率 36-44 %、自己間引きだけの
オフライン再現 43.3 % (実測 40.9 %)、重原子のみでの再現 100 %。

修正は 3 点。(a) 判定を重原子のみに。(b) コピーは完了してから参照 grid に併合。
(c) stacking period を公称 `leaflet` ではなく**実在する脂質の z 範囲**から導出
(パッチの脂質は z -26.87..27.89 = 54.76 A、公称は 46 A。差分が継ぎ目ごとの
真空になっていた)。

| | 水分子 | 追加 | overlap drop | 拡張領域の密度 |
|---|---|---|---|---|
| 修正前 | 32,149 | 17,338 | 23,819 | 28-43 % |
| (a)+(b) | 50,101 | 41,049 | 108 | 48-101 %、継ぎ目に 52 % の谷 |
| (a)+(b)+(c) | 54,726 | 40,950 | 495 | 77-100 % |

自由体積に対する充填率 54 % -> 91 %。原子数 186,516 -> 277,768。
**組んだ系の密度 0.625 -> 0.903 g/mL** (eq_001 と eq_003 の NVT レポータ実測)。
修正前は NPT が箱を 2438.7 -> 1812.3 nm^3 (-25.7 %) まで潰し、それでも 0.845 g/mL
止まりで上端に 18 A の空隙が残っていた。

### なぜ静かに壊れていたか

`solv` の geometry チェック、溶質の収容判定、塩濃度、電荷中性 — **全部通る**。
塩濃度は「間違った水量に対して正しく」計算されていた (32,326 水に 87 pairs で
0.150 M、55,027 水に 149 pairs でも 0.150 M)。**置いた材料の密度を見ている場所が
どこにもなかった。**

そこで `_membrane_embedding_geometry_report` に水酸素の z ヒストグラムを追加した。
既に全原子を読み `box_c` も持ちビルドを落とす権限もある関数で、1 パス増えるだけ。
判定は**絶対基準** (液体水 0.03344 molecules/A^3) で行う — 相対 (自セルの median 比)
では一様に薄い系を検出できない。実測で修正前は worst/median = 0.85 で通ってしまう。

```
solv_002 / solv_003 (修正前)  median 0.41 of bulk  -> failed
replay2  ((a)+(b))            median 0.958         -> passed
solv_004 ((a)+(b)+(c))        median 0.957         -> passed
```

### cursor レビューで追加で直したもの

- `membrane_zs` を「水でもイオンでもないもの = 膜」という消去法から Lipid21 名の
  積極的同定に変更。Mg2+/Ca2+ のような塩が bulk にいると膜と誤認し、span が箱
  いっぱいに広がって拡張全体が warning 一行で黙って skip されていた (これは (c) で
  私が入れた退行)。
- 拡張が必要なのに何も追加できなかった場合をハードエラー
  `membrane_patch_water_extension_failed` に。そのまま出荷すると 5L7D の元バグそのもの。
- copy 0 は変位がちょうど `patch_box_c` の格子像なので重なり判定を免除。
- resseq が 4 桁で溢れる前に chain label を繰り上げ (実測 9,343/copy、余裕 7 %)。

cursor が manifest から出した接触統計は 4 桁まで正しかった: POPC パッチの分子間
重原子最短は 2.1164 A で cutoff 2.2 A を下回るが、**水酸素間は 2.5264 A** なので
重原子判定は水について 15 % の余裕がある。一方 cursor の resseq 衝突の見積り
(1 コピー 10,350 分子、~1,000 件衝突) は実測 9,343 で**起きていなかった**。
箱外へのはみ出しも、この系では原子 z 範囲 173.70 が box_c 173.733 に収まっていた。

### 残っている限界

充填率 91 % で密度 0.903 g/mL。理想 (~1.0) より 10 % 低い。出どころは (1) interval
境界で分子単位に clip するため両端 3 A ほどが系統的に薄い、(2) 継ぎ目が bulk の
0.66 程度。NPT の穏やかな緩和で吸収される範囲なので今回は追わない。


## 2026-08-19 — MDDataBench の採点は甘すぎた。敵対的ベースラインで穴を 2 つ実測し、塞いだ

**「score が甘すぎないか」を議論でなく実測で確かめた結果、甘かった。** 落ちるべき提出を作って走らせたところ、
ランダム部分空間帰無だけでは 3 本が通ってしまった (D01 参照に対して):

| ベースライン | RMSIP | z | 当時の判定 |
|---|---|---|---|
| ANM 低振動モードからのサンプル (**MD ゼロ**) | 0.515 | 46 | **通過** |
| 本物の MD を 100 ps に切り詰め | 0.627 | 60 | **通過** |
| 本物の MD を 10 ps に切り詰め | 0.420 | 36 | **通過** |
| 結晶構造 + 等方ノイズ | 0.130 | -0.5 | 正しく失敗 |
| 最小化構造の複製 | 0.135 | 1.7 | 正しく失敗 |

つまり**「正しい分子か」は証明できていたが「実際に走らせたか」は証明できていなかった**。
`production_ran_for_one_nanosecond` がノード自身のメタデータを読むだけだったのも同根。

**修正 1: 構造のみの床 (ANM) を追加。** 結晶構造から組んだ弾性ネットワークが RMSIP 0.57 に達するので、
それを margin 0.05 付きで超えることを要求する。床はカットオフ最大化で取る (カットオフは攻撃者の自由変数)。

**修正 2: 経過時間を物理で検証。** 拡散係数は**強度量**で使えない (同じ軌道の 1 ns と 100 ps でどちらも
3.7e-5 cm^2/s)。**連続 unwrap した溶媒の総変位は示量量**で、999 ps から 989 ps、99 ps から 98 ps、
9 ps から 15 ps を復元した。溶媒が無い提出は計測不能で即失敗 = MD ゼロ提出に対する正しい判定。

修正後、5 本すべてが失敗し実 run だけが通る。**D01 の 100 ps は ANM 床を 0.005 差で超えてしまい、
時間検証だけが捕まえた** ので 2 つとも要る。恒久回帰として `scripts/negative_controls.py` を追加。

**採点を prep と md に分割 (ユーザ指示)。** 単一の数では「組み立てで落ちた」のか「シミュレーションで
落ちた」のかが言えない。ANM 提出と 10 ps 提出はどちらも **prep 満点・md 失敗**になり、帰属が機能する。
D01 prep 7/7 md 6/6、D02 prep 8/8 md 6/6。

**副産物のバグ 1 件.** `negative_controls.py` 初版が原子インデックスをファイル行番号で数えており
(トポロジにはヘッダ行がある)、real_full_run が 0.688 と出て scorer の 0.729 と食い違った。修正後一致。
**scorer と回帰ハーネスで同じ値が出ることを毎回突き合わせる**のが早期発見に効いた。

---

## 2026-08-19 — scorer 修正、D02 追加、プロンプト最小化

**scorer の偽 FAIL 2 件を修正。** `benchmarks/mddatabench/scripts/score_submission.py` として
実装し直した。ハードコードした `True` を廃し、全項目を artifact から再計算する。正しい所在は
`amber_metadata.json :: parameters.water_model` (+ `forcefield_provenance.openmm_xml`) と
prod ノードの `metadata.system_signature.ensemble`。**バロスタットは実行時に付与されるので
topo ノードの system.xml には無い。** 契約原子は生インデックスではなく (残基番号, 原子名) で対応付ける
(提出側トポロジは溶媒を含む)。D01 で 11/11 を再現。

**D02 を追加: MDDB `A00AJ` (MoDEL 1CSP、枯草菌 major cold-shock protein CspB)。**
MDPrepBench との交差を機械的に取った結果 (MDPrepBench 30 PDB 中、CC + Classical MD + 解析5種を
満たすのは 1UBQ / 1CSP / 2CBA / 1BNA)。**2CBA は見送った** — MoDEL の寄託に Zn が入っておらず
(HETATM ゼロ、apo)、触媒金属を捨てることを報酬にしてしまう。MDPrepBench P26 の趣旨と正反対になる。

D02 が D01 に足す能力は**側鎖補完**と**非ゼロ溶質電荷**。PDB 1CSP は Glu 3/21/36/66 の側鎖先端を
欠いており重原子 505、MDDB 参照は 521。**505 + 16 = 521 という等式を参照自身が与える**ので、
正解をキュレータが決めなくてよい。参照スケールは 201 原子 (67×3)、1 ns 窓どうし 0.687 ± 0.035、
帰無 sqrt(10/603)=0.129。

**プロンプトを最小化した (ユーザ指示)。** scorer が両側のサブスペースを自分で計算するので、
**エージェントに解析させる必要がそもそも無い**。解析契約・報告項目・鎖選択・互変異性体・箱形状・
側鎖補完の指示をすべて削除し、残したのは「PDB ID / TIP3P / 中性化 / 300 K / NPT / 1 ns 以上」だけ。
参照バンドルは**ソルバのワークスペースに一切置かない** (採点時に評価器が取得する) ので、
参照が漏れる経路が消えた。

**簡素プロンプトが解けることを実測で確認。** プロトネーションの指示ゼロで MDClaw は
1UBQ -> 602/1231、1CSP -> 521/1014 と参照組成に厳密一致し、1CSP の Glu 4 残基を無指示で補完した。
2 つの run は参照と**逆の**互変異性体を選んだが (1UBQ で HID、参照は HIE / 1CSP で HIE、参照は HID)、
重原子数は互変異性体に依らないので設計どおり通る。総原子数には ±2 の許容を入れた。

**採点結果**: D01 11/11 (RMSIP 0.729, z 72.4)、D02 12/12 (RMSIP 0.703, z 64.0)。
どちらも方向は復元、振幅は 2-6 倍小さいという同じ姿。ruff clean、リポジトリ内のデータは 0 バイト。

---

## 2026-08-19 — D01 を MDClaw で実際に解いて 11/11。試走が scorer の欠陥を 2 件出した

同日前エントリで作った MDDataBench D01 を、MDClaw 0.6.6 で最初から最後まで解いて採点した。
A6000 1 枚、1UBQ chain A -> ff14SB + TIP3P、cubic 15 Å、31355 原子、HMR 4 fs、
NVT 100 ps + NPT 200 ps、**1 ns NPT production が 2 分 29 秒**。

**結果 11/11 PASS.** 核となる検定は RMSIP=0.729、z=72.4、p<5e-5 で H0 棄却。
正準相関 10 本すべてが帰無 99 パーセンタイル超。Rg=1.1777 nm（参照 10 ns 平均 1.1807、
差 0.0030 は 1 ns 窓の SD 0.0102 の 1/3）。prep 出力は重原子 602 / 全 1231 / 76 残基 / HIE で
参照と完全一致した。

**設計の予測が当たった。** 固有値比 (own/ref) は [0.60, 0.35, 0.18, 0.18, 0.29, ...] で、
**方向は復元できるが振幅は 2-6 倍小さい**。τ(PC1)=1236 ps から予測したとおり 1 ns では
遅いモードの分散が出ない。RMSIP 0.729 は ANM 床 (0.47-0.62) を超え、参照自身の 1 ns 窓間
自己一致 0.760 ± 0.053 の 0.6σ 以内。**別力場 (ff14SB vs Parm99) の独立な 1 ns が、
参照自身の 1 ns 再現性と同じ水準に着地した。** 「H0 棄却は採点、定量一致は採点しない」
という設計判断が実測で正当化された。

**試走で出た scorer の欠陥 2 件（いずれも偽 FAIL）.**

1. water model を `amber_metadata.json` 直下で探したが実際は `parameters.water_model`
   (+ `forcefield_provenance.openmm_xml` に `amber/tip3p_standard.xml`)。
2. barostat を topo ノードの `system.xml` で探したが、**バロスタットは実行時に付与される**ので
   そこには無い。ensemble は prod ノードの `metadata.system_signature.ensemble` を読む。

さらに、参照の 228 契約原子は**生の原子インデックスではなく (残基番号, 原子名) で対応付ける**必要がある
（提出側トポロジは溶媒を含むのでインデックスが一致しない）。これらを `task.json` の
`scorer_field_map` に記録した。**artifact-as-truth を掲げても、artifact のどこに何があるかを
実走で確定しないと scorer は嘘をつく。**

`benchmarks/mddatabench/scripts/evaluate_submission.py` を追加。ruff clean。

## 2026-08-19 — 膜修正込みの arm64 SIF を焼き直した (5c7e89590560)

`93ebfa5..5c7e895` の膜まわり4コミット (leaflet 両側からの midplane、蛋白フレーム上での
bilayer 配置、既存塩のカウント、solute からの z セル寸法、preview の説明) を取り込んだ
状態で再ビルド。

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-5c7e89590560
sif     ~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-5c7e89590560.sif
        6,774,202,368 bytes
        SHA-256 1c5e3fe5c32b94a6f03aed3ef79ccecb0a38e277f8675551593824d82ad8be9e
smoke   Docker 23/23、SIF からも 23/23 (GPU は SKIP)
```

今回は GHCR に push していない。`docker save` した tar を Lima VM `singularity-ce` に
読ませて `docker-archive:` から変換したので、外部公開なしで完結している。tar と VM 側の
scratch は変換後に削除済み。

なお**この4コミットは `mdclaw/` の Python だけで、コンテナの中身 (environment.yml /
container/ / pyproject.toml) は変わっていない**。つまり SIF の作り直しは必須ではなく、
Rikyu 上で既存 SIF に `PYTHONPATH="$PWD"` overlay を掛ければ同じ挙動が得られる。今回は
明示の依頼で焼いた。逆に `mdclaw/` を 1 行でも変えて再ビルドすると stage 1 の
`COPY mdclaw/` でキャッシュが切れ、conda 環境の作り直しから丸ごと走る (実測 ~50 分 +
SIF 変換 ~10 分)。修正はまとめてから焼くのが得。

Rikyu 実機での GPU smoke は引き続き未実施。

---

## 2026-08-19 — MDDataBench D01 を作成: RMSIP による「無関係」帰無仮説の検定

`benchmarks/mddatabench/` を新設し、最初のタスク D01 (1 ns MD + 本質サブスペース一致) を実装・検証した。
参照は MDDB `A0142` (MoDEL 1UBQ、CC-BY 4.0、Amber Parm99 / TIP3P / 300 K / NPT / 10 ns)。

**採点の核: H0 =「2 つの本質サブスペースは無関係」を RMSIP で棄却する検定。**
ランダム直交フレームの Monte Carlo で帰無分布を作る (M=20000 で平均 0.1206 / SD 0.0083、
解析値 sqrt(D/3M)=0.1209 と一致)。**力場校正が不要**なので、rev.2 の「未校正の量に閾値を置かない」
規律を破らずに MD 部分を採点できる。実測 (D=10, 3M=684):

| 比較 | RMSIP | z | 棄却 |
|---|---|---|---|
| ランダム (負の対照) | 0.121 | 0.0 | **no** |
| ANM (構造のみ, 10 A) | 0.617 | 59.5 | yes |
| 座標系ズレ (大域回転) | 0.652 | 64.2 | yes |
| 1 ns 窓 vs 1 ns 窓 | 0.760 ± 0.053 | 81.8 | yes |
| 1 ns 窓 vs 全 10 ns | 0.794 ± 0.029 | 84.7 | yes |
| 10 ns から 500 フレーム | 0.969 | - | yes |

**この検定は妥当性ゲートであって品質スコアではない。** 構造だけから作った ANM も H0 を棄却するため、
「正しい分子を正しい契約で解析したか」は保証するが「サンプリングが収束したか」は保証しない。

**1 ns では上位モードを定量比較できないことが判明。** 参照の積分自己相関は PC1 1236 ps / PC2 1081 ps /
PC4 1660 ps で、**1 ns 中の独立標本は PC1 で 0.8 本**。10 ns の参照でも 8 本。Marchenko-Pastur は
q_eff = N/T_eff が 1 ns で 188、10 ns でも 38 となり適用不能 (PRL 103, 268101 (2009) の手法は
MP 上端ではなくバルクの準位間隔統計)。よって連続値 RMSIP は診断のみとし、校正データとして蓄積する。

**解析契約が必須であることの数値的裏付け.** MDDB は PCA の固有値と射影を配信するが**固有ベクトルは配信しない**
ため、scorer 側で再計算が必須。契約 `pca_backbone_subspace@1` (主鎖 N/CA/C 228 原子、参照構造への Kabsch
フィット + running mean 3 反復、D=10、Å) で公開固有値を -4.8% 〜 +3.4% で再現。摂動の効き方は
大域回転 -0.175 > 原子順序 -0.018 > 平行移動 0。なお Rg では標準原子量の質量加重が公開値と
+0.0024 nm 系統的にずれ、これは 1 ns 窓の SD 0.0102 nm の 24% に相当した。

**データは非同梱.** `scripts/fetch_reference.py` が MDDB から取得し provenance と SHA-256 を書く。
再取得でバイト一致を確認済み。`.gitignore` で取得物のコミットを禁止。solve 時は `mddbr.eu` を遮断、
RCSB は許可。プロンプトに accession を出さない。

**Rg を主観測量にする案は棄却した.** 正しく作れば誰でも 1.18 nm になり識別力がない。RMSIP は
0.12 (偶然) - 0.79 (1 ns 自己一致) - 1.0 と広いレンジを持つ。ruff clean、取得から検定まで通し検証済み。

---

## 2026-08-19 — 実物の図を見て膜構築のバグが 5 件出た。うち 3 件は塩とイオン配置

hackathon の報告 (「5L7D を膜に埋めたら細胞外ドメインが箱からはみ出た」) から始めて、
**PyMOL で組み上がった系を実際に描いた**ところ、箱サイズ以外に 4 件のバグが出た。
どれもエラーを出さずに通っていた。cursor に 2 回レビューさせ、指摘 2 件も反映した。

### 1. 二重層が膜貫通領域を囲んでいなかった

`_estimate_patch_membrane_center_z` は頭部基の**周期平均**で midplane を出していた。
`_periodic_mean` は最大の隙間で展開するが、二重層には**疎水コア (~34 A) と水層 (~35 A)**
という同程度の隙間が 2 つあり、コア側で展開すると**水層の中点**、つまり真の midplane から
**半箱ずれた点**を返す。

同梱 POPC パッチ (box_c 76.846) の実測: phosphate 面 26.4 / 71.2、真の midplane 48.8、
**推定値 10.3**。差 38.5 = 箱の半分。結果、組み上がった系では二重層が z=38.5 に座り、
OPM 転写が二重層内に置いた 173 残基のうち**0 残基**しか膜内に入っていなかった。
**脂質が細胞外ドメインに巻きつき、TM ヘリックスは水の中**にいた。

アシル鎖から推定するよう変更 (コアは連続した 1 枚なので展開が一意)。推定値 48.5。

### 2. 配置ガードが機能しない設計だった (cursor 指摘)

修正1 のあとに「配置後にもう一度推定して目標と比べる」ガードを足したが、cursor に
**代数的に無意味**と指摘された。shift = target − E(atoms) で E が並進同変なら
E(atoms + shift) = target が恒等的に成立する。**推定器が半箱ずれていても必ず通る。**

推定器を使わない独立判定に置換した。目標フレーム位置での **(a) アシル鎖炭素の存在、
(b) 水の不在、(c) 頭部基が両側にありバランスしていること**。実測:

```
壊れていた系: tail 0    / water 4252 / 頭部基 下0/上440   -> 棄却
直した系:     tail 3604 / water 0    / 頭部基 下210/上220 -> 通過
```

証拠が足りないとき (スタブパッチ等) は判定しない。無い証拠で拒否するのも誤答なので。

### 3. タイルの分子識別子が重複していた — 塩濃度が 1/(nx*ny)

`build_tiled_membrane` は**行だけ**書き換えて `PDBAtom` レコードを書き換えていなかった。
中和は `_water_residues` が `atom.chain_id`/`atom.resseq` をキーに分子をまとめるので、
**6 枚のタイルにある「パッチの水 #1」が全部同じキーに潰れる**。

- `n_water = len(water_groups)` が塩濃度計算に使われるので、**バルク塩が 1/6 しか入らない**
- 候補プールも縮み、イオンが偏在した (実測 下26/上88、水は 1:1)

`replace()` で識別子を持たせて修正。修正後 下77/上85 (水 下7491/上7381)。
私が水拡張で作った同種のバグを直したとき、元からタイリング側にもあることに
気づくべきだった。

### 4. 中和が z 順に種を割り当てていた — 周期箱を横切る電荷分離

候補を z 昇順にソートしたうえで**陽イオンを全部先に、陰イオンを全部後に**割り当てていた。
実測 chain I は NA 下5 / CL 下23 / CL 上4。交互配置に変更。

### 5. 塩が二重に入っていた (cursor 指摘)

キャッシュ済みパッチは**要求濃度で詰められている**のに、`plan_neutralizing_ions` が
既存イオンを引かずに満額を追加していた。修正3 で `n_water` の過小評価が直った結果、
**0.268 M (要求 0.150)** になって顕在化。

種ごとの不足分だけ追加するよう変更 (対で数えると、埋没イオン除去で片側だけ減ったとき
「対は 0」と読んで満額追加してしまう)。修正後 **0.151 M**、正味電荷 −37 (蛋白の中和ぶん)。

### 付随: 同梱パッチの二重層内にイオンが埋まっている

同梱 POPC パッチには二重層の中に Cl− が入っており、タイル 6 枚ぶん複製されて
膜内イオン 25 個になっていた。タイリング時に除去し、中和で bulk に置き直す (30 個除去)。
**パッチ自体の欠陥**なので、パッチを作り直すのが本筋。

### 可視化 — これが無ければ 4 件とも見つかっていない

`render_structure_preview` に `system_box` スタイルを追加。蛋白 chain 別 cartoon、
脂質 stick、水は半透明 surface、イオン sphere、**周期セルをワイヤ枠**で描く。
orthographic、x 軸と z 軸の直交 2 視点。セルは**水+脂質の重心**に描く (系全体の重心だと
箱から出た蛋白に箱が引きずられて意味を失う)。

cursor 指摘で `orthogonal_view` を `system_box` 限定にした。全スタイルに効いていたため
`ligand_site` などが本来のカメラを失っていた。

skill 側は `visual-qa.md` に「系を変える各ステージで描画し、**画像をユーザに送る**」、
`run-loop.md` に「各ステップ後に node ID・実際に使われた条件・系のサイズ・warnings を
報告する」を追加。**描いてもユーザに見せなければ描いていないのと同じ**。

### 既存成果物への影響

D473Y / G497W の既存系はすべて (1)(3)(4)(5) の影響下にある。D473Y は solv まで作り直した。

### cursor の P2 指摘を反映

| 指摘 | 対応 |
|---|---|
| tail 平均が鎖長・リーフレット組成で偏る | `_leaflet_midpoint` を実装。両リーフレットを個別に求めて**重み無し**中点。実パッチで 48.7 (真値 48.8、tail 平均は 48.5) |
| コレステロール単独パッチを扱えない | 頭部基 → tail → 全脂質原子の順にフォールバックし、いずれも判別器を通す |
| 5 A 許容値が不適切 | ガードを密度判定に置換したので定数ごと削除 |
| `solvent_ions` が surface の上に dots を重ねる | 汎用ブロックは dots に戻し、surface は `system_box` 専用に |
| `system_box` が `show_lipids`/`show_ions` を無視、manifest と PNG が食い違う | フラグを尊重し、**実際に描かれた表現を PyMOL から読み戻して** manifest に記録 |
| 周期セルが三斜箱を直方体として誤描画 | α/β/γ を検査し、直方体でなければ描かずに理由を記録 |
| manifest / node metadata に 2 枚目の画像が無い | `views` (軸と画像パス)、`periodic_cell`、`output_png_top` を記録 |
| skill の記述が矛盾、prep に `system_box` を指定 | 「描画の**試行**は毎ステージ、成功は best-effort」。prep は `overview`、solv 以降が `system_box`。2 枚とも見るよう明記 |

判別器も改良した。頭部基だけでは「膜の中点」と「水の中点」が等価なので、
**アシル鎖の存在と水の不在の両方**で決める (水だけだと合成パッチで判別できなかった)。

CLI 側も埋めた。`render_structure_preview` の docstring にスタイル一覧・`system_box` の
描画内容・2 視点・「ユーザに送ること」を書き、`docs/developer/tool-reference.md` も更新。

最終確認 (solv_016): 二重層 midplane 0.1、膜内イオン 0、塩 **0.150 M** (要求 0.150)、
イオン 下59/上68 (水は 1:1)、geometry passed。

### 未対応

- タイリング由来の人工的周期性 (イオン 68 個が 28 箇所の xy に重なる、同一脂質配置の複製)
- **同梱パッチ自体の作り直し** (二重層内にイオンが埋まっている)
- G497W の再構築、D473Y も solv 止まり

---

## 2026-08-19 — バグ: patch-tile が z 方向の box サイズをタンパク質から決めていなかった

hackathon メンバーから「GPCR 5L7D を膜に埋めて水和したら、細胞外ドメインが box の
両側からはみ出た」との報告。**既定バックエンド `patch-tile` の実バグだった。**
cursor にも独立検証させ、4 点すべて追認された (指摘 3 点は私の説明の補正)。

### 根本原因

`patch_membrane.py:636`

```python
box_c = 2.0 * (float(dist_wat) + float(leaflet))   # 溶質が一切入らない
```

`patch_membrane.py` の組み立て:

```python
total_box = {"box_a": nx * box_a, "box_b": ny * box_b, "box_c": box_c}
```

`nx`/`ny` は `_tile_counts` がタンパク質の XY 境界 + 2*dist から決める。
**z には対応物が無い。** `_tile_counts` は `_bounds()` の `_minz, _maxz` を明示的に
捨てている。箱の高さの取得元は 3 つ (キャッシュ / patch の CRYST1 / 導出式) あるが、
**どれもパッチの高さであって溶質とは無関係**。

### 5L7D 実測

```
5L7D chain A 膜フレーム          z = -30.3 .. +77.9 A   (span 108.2 A)
既定 box_c = 2*(17.5+23.0)                             =  81.0 A
build_amber_system の +2 A margin 後                   ~  83.0 A  <- MD の実箱
箱外の原子              3754 中 1131 個 (30.1%)
最小イメージで折り返す先                               z in [-40.5, -3.1]
うち周期像の脂質コア (|z|<15) に入る原子                240 個
```

**細胞外ドメインが隣の周期像の膜を貫通している。**

### なぜ静かに壊れるか

1. **carve が PBC 対応** (`patch_membrane.py:1795`) なので、折り返した CRD の周りの脂質が
   「正しく」除去され、**膜に穴が空いた系がエラー無しで完成する**。
2. **geometry チェックが検出できない** (`membrane.py:327`)。見ているのは headgroup span と
   「タンパク質原子の 15% 以上が膜と交差するか」だけ。5L7D は TM 部分で余裕で通る。
   溶質の z 範囲も箱面までのクリアランスも計算していなかった。

### MD まで伝播する

`total_box` → CRYST1 + `box_dimensions.json` → `build_amber_system` →
`openmm_build.py:1037-1060` の `setPeriodicBoxVectors` → `system.xml` / `state.xml` →
min/eq/prod が state の箱ベクトルを優先採用。`center_solute_and_wrap_solvent` は
最大分子を意図的に wrap しないので形は保たれるが、83 A の箱に 108.2 A の分子は入らない。

### packmol-memgen 経路は無事

上流ソース (`packmol_memgen/main.py`) は

```python
z_max = pdbz_max + distance_wat
if z_max < (leaflet_z + distance_wat): z_max = leaflet_z + distance_wat
```

と溶質と膜の**大きい方**を採る。CLI ヘルプも "water layer over the membrane **or protein**"。
`patch-tile` が名前だけ借りて「膜からの厚み」として実装したのが分岐点。
なお `membrane_backend` の正しい値は `packmol-memgen` / `patch-tile` / `auto` で、
`full` は存在しない (私が最初にそう書いたのは誤り)。

### 修正 (恒久対応)

**`dist_wat` を packmol-memgen と同じ「膜または溶質のうち遠い方からのパディング」に
定義し直す。** (当初はパッチ自体を高くしたが、下記のとおりやり直した。)

```python
effective_dist_wat = max(dist_wat, max|z_solute - centre| + dist_wat - leaflet)
```

5L7D では 17.5 → **72.4**、box_c 81.0 → **190.8 A**。TM のみの蛋白では 17.5 のまま
(回帰なし)。

**箱だけ広げる案は採らなかった。** 水を足さずに CRYST1 を伸ばすと真空層ができて
密度が壊れる。パッチを高くすれば、追加された体積は平衡化済みの水で正しい密度のまま埋まる。
キャッシュは `dist_wat` を指紋に含むので、高いパッチは自動的に別エントリになり、
**スキーマ変更は不要**。代償は「背の高い蛋白の初回 cold build が長い」ことで、
cold-build notice にその旨を出すようにした。

対称箱 (190.8 A) を採り、非対称最小箱 (143.2 A) は見送った。非対称にするには
平衡化済みパッチを任意面で切る必要があり、切断面同士は真の周期対応ではないので
継ぎ目が生じる。水量は約 33% 多いが、継ぎ目の無い正しい系を優先した。

保険として 2 つ追加:
- 組み立て後に `solute_fits_box` で封じ込めを検査し、入らなければ
  `membrane_patch_solute_exceeds_box_z` で拒否 (古いキャッシュ由来の高さ対策)
- geometry レポートに `protein_exceeds_periodic_box_z` を追加。膜との交差判定とは
  **別の失敗理由**にしたので、「膜には正しく入っているが箱に入っていない」を検出できる

### cursor レビューで自分の修正に P1 回帰が出た

**封じ込め判定を「膜中心 ± box_c/2」に対する原子位置で書いたのが誤り**だった。これは箱が
膜中心に対して対称であることを前提にしており、packmol-memgen が作る非対称箱では偽。
cursor が再現したとおり、108.2 A の溶質が入る 143.2 A の箱を「6.3 A はみ出し」と誤判定して
落とす。`auto` で fallback した後にこれが走るので、**正しく作った系を落とす回帰**だった。

PBC では原点は任意で、面をまたいだ分子は反対側から入り直すだけ。平行移動で消せないのは
**分子が周期長より長い**ことだけ。判定を `protein_z_span >= box_c` に変えた
(`solute_fits_box` も同様。中心が不要になったので `membrane_center_z=None` の二重解釈と
いう別の指摘も同時に解けた)。

### さらに設計をやり直した: パッチを高くするのではなく、水を足す

ユーザから 2 点の指摘を受けた。**どちらも正しく、最初の実装は誤りだった。**

1. 「キャッシュが効くようにできないの？」
2. 「23 A なんて多くの膜タンパク質が対象になると思うけど」

拡大条件は `reach + dist_wat - leaflet > dist_wat`、すなわち **`reach > leaflet` (23 A)**。
**|z| が 23 A を超える膜蛋白はほぼ全部が対象**になる。そして `dist_wat` はキャッシュ指紋に
入っているので、**そのたびに cold build が走る**。実際、2LOP のパイプラインテストが
通常 ~100 秒のところ **20 分以上** cold build を回していた。

**膜パッチの高さを溶質に依存させたのが誤り**だった。二重層は蛋白と無関係なので
キャッシュしたまま使い、**足りない水だけを z 方向に足す**のが正しい:

```python
low  = min(solute_z_min, centre - leaflet) - dist_wat
high = max(solute_z_max, centre + leaflet) + dist_wat
```

パッチは常に呼び出し側の `dist_wat` で要求するのでキャッシュは必ずヒットする。
足りない体積は**パッチ自身の水スラブのコピーを z 方向に積んで**埋める。溶媒和プログラムが
平衡化済み水ボックスを複製するのと同じやり方で、密度もイオン濃度もそのまま乗る。
コピー同士の境界は真の周期対応ではないので、既存原子と 2.2 A 以内に来た分子は
**丸ごと落とす** (これも溶媒和プログラム標準の重なり除去)。

**箱は非対称最小になった。** 5L7D で **143.2 A** (対称なら 190.8、バケット化ありなら 206)。
細胞外ドメインぶんの水を膜の下側にミラーする必要が無くなったので、水量も減った。

実データ検証 (5L7D 実座標 + 合成パッチ):

```
interval        low=-47.8  high=+95.4  box_c=143.2  (extend below 7.3 / above 54.9)
extension       18252 分子追加、重なり棄却 0
assembled z     -45.1 .. 94.5
containment     fits=True  span=108.2  headroom=35.0
水の数密度       元スラブ 0.0330 /A^3  →  拡張部 0.0329 /A^3
```

**パイプラインテストは 97.79 s / 4 passed に戻った** (cold build 20 分超 → キャッシュヒット)。

なお高さバケット化と `membrane_patch_box_too_tall` の上限は、パッチを高くしなくなったので
不要になり削除した。

### テスト

`tests/test_solvation_server.py` に 4 本追加 (5L7D の実数値で拡大を検証 / TM のみは
拡大しない / 箱に入らない溶質を拒否 / 膜判定は通るが封じ込めで落ちる geometry ケース)。
レビュー対応後に 3 本追加 (非対称箱を受理する / 上限超過を拒否する / 高さバケット)。
ruff clean、solvation + guardrail + contract + orientation 系 321 passed。

---

## 2026-08-19 — arm64 イメージに PPM3 を移植。amd64 の検証は無効、MODELLER の ldd は初実走で自壊

Rikyu 用 SIF を作り直すため、`Dockerfile.rikyu-arm64` を Mac (Apple Silicon /
Docker Desktop) で再ビルドした。amd64 に入っている PPM3 パッチが arm64 側に未移植
だったので、それを移す作業。移すだけのつもりが、両方の**検証**が壊れていた。

### aarch64 の conda 版 immers も同じバグを持っている

移植前のイメージ (`54798ff`) を調べたところ、`/opt/mdclaw/bin/immers` は**既に存在
する**。ppm3 のソースディレクトリには binary が無く、ambertools の conda パッケージが
aarch64 ビルドを bin に置いている。中身:

```
' tilt=',f7.0'+-',        <- カンマ欠落。amd64 の同梱バイナリと同じ
```

なので arm64 でも「パッチして make し直し、conda 版を上書きする」が必要だった。
`install -m 0755 ... /opt/mdclaw/bin/immers` はその上書きになる。

### amd64 の post-check は一度も発火していない

```
/opt/mdclaw/bin/immers < /dev/null 2>&1 | grep -q "Fortran runtime error: Missing comma"
```

空 stdin だと `opm.f:84` の最初の read で "End of file" で死ぬ。**問題の FORMAT 行に
到達しないので、このパターンは絶対に一致しない。** さらに `sed && grep || true` の連鎖
なので、sed が当たらなくても `|| true` に飲まれてビルドは続く。つまり amd64 側は
「パッチが当たらなくても素通りする」状態。

arm64 版はコンパイル済みバイナリ内の FORMAT 文字列で判定するようにした。パッチ後は
`f7.0,'+-'`、未パッチは `f7.0'+-'` が入っているので、これは実際に区別できる。空 stdin
で走らせる方は残したが、意味は「バイナリが共有ライブラリを解決して Fortran ランタイム
まで到達する」ことの確認に変えた。同じ判定を `test-container.sh` にも入れ、
`MDCLAW_PPM3_PATCHED` を宣言したイメージにだけ効かせる (古い SIF は SKIP)。効き目は
古いイメージに変数を立てて確認済み: 20 passed / **1 failed**。

### MODELLER の ldd 検証 (8afd86e) はイメージではなく自分が壊れていた

初回ビルドは stage 2 の 19/20 で落ちた。

```
libglib-2.0.so.0 => not found
libmodeller.so.14 => not found
```

どちらもイメージ内に実在する (glib は conda の `/opt/mdclaw/lib`、libmodeller は拡張の
隣)。`ldd` をランタイムの `LD_LIBRARY_PATH` 無しで走らせていたのが原因で、**検査の欠陥
であってイメージの欠陥ではない**。2026-08-18 のエントリで「rikyu の end-to-end ビルドは
未検証」と書いた通り、この検査は今回が初の実走だった。ランタイムが宣言しているのと同じ
検索パスを与えて解決。

### 結果

```
image   ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-f9e628126877  21 GB
digest  sha256:32fde85be54f4582a13129808092881af4ff5fcb225b7564aa23bb3797475ddf
smoke   23 passed, 0 failed   (PPM3 と MODELLER を含む。GPU は SKIP)
```

GHCR に push 済み。パッケージは public で、匿名トークンで manifest を引けることを確認
したので、Rikyu 側は資格情報なしで `apptainer pull` できる。

SIF は手元の Lima VM `singularity-ce` (singularity-ce 4.5.0) で digest 指定 pull から
変換した。

```
~/Downloads/mdclaw-rikyu-arm64-cuda130-ppm3-f9e628126877.sif
6,774,157,312 bytes
SHA-256 367af38cb733207176703d69b5d115f629707565db40ebf8ad93befb2947d8e4
```

**SIF からも container test 23/23。** PPM3 チェックが SIF 内で通ったことには意味があり、
再ビルドした `immers` が (apt の gfortran ではなく) conda の libgfortran.so.5 を
ランタイムの LD_LIBRARY_PATH 経由で解決できていることの確認になっている。残るは Rikyu
実機での GPU smoke (`test-rikyu-gpu.sh`) で、これは SIF からでないと FUSE 経路を踏まない。

### Mac でビルドできる

`build-rikyu-arm64.sh` は arm64 host なら通るが、`nproc` と `df -BG --output` が GNU
限定で macOS では動かなかった (前者は `set -e` でその場で死ぬ)。`sysctl -n hw.ncpu` /
`df -k` へのフォールバックを入れた。Docker Desktop の VM は 14 CPU / 8.3 GB なので
`BUILD_JOBS=6` に絞った (並列 nvcc はメモリを食う)。SIF 化だけは Mac では出来ない
(apptainer が無い) ので、GHCR に push して Rikyu 側で `apptainer pull` する。

---

## 2026-08-19 — cursor 再レビューで 10 件。coplanar 棄却と sparse-perfect の順位が実バグ

同じ `smo_reviewer` に修正後の差分を再レビューさせた。**今回はファイルを一切変更していない**
(前回の違反を依頼文に明記し、テスト提案はレビュー文書内に書くよう指示した)。
新規指摘 10 件 (P1×2, P2×6, P3×2)。**設計を変える 2 件は自分で再現してから直した。**

### 実測で確認したこと

```
[coplanar]  rmsd=4.5e-15  det=1.000  max|R-Rtrue|=3.3e-16  fit_condition=0.0000  <- 正しい fit を棄却
[collinear] s2/s1=0.0000  s3/s1=0.0000
[1 helix]   s2/s1=0.0947  s3/s1=0.0929
[rank]      sparse (1.0, 0.2, -2.9) > broad (0.99, 1.0, -0.1)   <- 40/40 が 198/200 に勝つ
[validate]  max_candidates=1.5 -> None                          <- 小数が通る
[short ATOM] RAISED IndexError: string index out of range
```

### 訂正1: 縮退判定は rank-2 で十分だった (私が厳しすぎた)

`_fit_condition` を s3/s1 (最小/最大主成分) にしていたが、**これは Kabsch の可同定条件より
厳しい**。非共線な 3 点以上あれば面内基底 2 本が決まり、**proper rotation 制約が法線を決める**。
実際、完全に共面な 40 点で一般回転を **3.3e-16** の精度で復元できるのに、私のゲートは
`fit_condition=0.0000` でこれを棄却していた。**s2/s1 に変更**: 共線 0.000 / 共面 1.000 /
単一理想ヘリックス 0.095 / 実膜 CA 集合 0.86-0.87。しきい値 0.01 は据え置き。

### 訂正2: identity の丸めでは sparse-perfect を止められない

前エントリで「支持量を最良候補比の 1/10 に丸める」ことで 201 対 200 問題を解いたが、
**identity を先頭キーに置いたままだと 40/40 (100%) が 198/200 (99%) に勝つ**。
40 観測の 100% は 200 観測の 99% より*弱い*主張である。
**Wilson 下限**に置き換えた: 40/40 → 0.91、198/200 → 0.96、200/200 と 201/201 → ともに 0.98。
2 桁に丸めれば 201 対 200 も同値になり RMSD が決める。キーは
`(round(wilson_lb(membrane_identity, membrane_ca), 2), -fit_rmsd)` の 2 本になり、
支持量バケットは Wilson が吸収したので削除した。

### その他の修正

| # | 内容 |
|---|---|
| P1 | **全 query chain を採点してから 1 本のランキングで決める**。従来は「最初に受容可能な donor を持った鎖」が勝っていた。長い膜結合パートナーが辛うじて通る donor で複合体全体の配向を決め、本命の膜貫通サブユニットが使われない |
| P1 | **予算切れで候補が残ったら「最良」と主張しない**。ゲートを全部通った donor は採用する (別手法に落ちるより良い) が、`evaluation_complete=false` と warning を出す |
| P2 | **altLoc は残基単位で 1 つ選ぶ**。原子ごとに occupancy 最大を取ると CA が conformer A、CB が B という**実在しない混成側鎖**ができる |
| P2 | **TER を保持**。落とすと 2 本のポリマーが 1 鎖に融合し、無い結合ができる |
| P2 | **不完全評価に専用コード** `opm_homolog_evaluation_incomplete`。「1 件棄却 + 1 件 DL 失敗」を `rejected` と報告すると、エージェントは「調べた結果ダメだった」と解釈して再試行しない |
| P2 | **54 桁未満の ATOM を構造化して弾く** (従来は IndexError が外に出ていた) |
| P3 | **カウント系は整数必須**。`max_candidates=1.5` が RCSB に不正ページ要求として届き、依頼者のミスが「検索障害」として返ってきていた。`min_fit_condition=0` も禁止 |
| P3 | **予算の 1 秒下限を撤去**。短い明示予算が拘束力を失う |

### 予算切れ時の扱いはレビュー提案と変えた

レビューは「切り詰められた候補集合からは選ぶな (= PPM3 に落とせ)」としたが、
**全ゲートを通った donor を捨てて別手法に移るのは利用者の不利益**と判断した。採用したうえで
`evaluation_complete=false`、warning、`report` への記録で「比較が不完全だった」ことを明示する。
一方、**何も受容できなかった場合に `rejected`/`no_match` を返すのは誤報**という指摘は全面的に
正しいので、そちらは `opm_homolog_evaluation_incomplete` にした。

### ライブ再測定

修正後も 5L7D で **5L7D 自身を採用、PDBTM 法線誤差 5.9 度** (変化なし)。
mock 版 4JKV 経路も 11.8 度で不変。

### テスト

ruff clean。`test_membrane_orientation.py` 62 → **75** 本。contract 系込み 467 passed。

---

## 2026-08-19 — cursor レビューで 10 件。うち 5 件は再現、ランキングの欠陥も実測で露見

`smo_reviewer` (cursor / GPT-5.6 Sol) に未コミット差分をレビューさせた。指摘 10 件
(P1×2, P2×6, P3×2)。**再現できるものは全部走らせ、5 件すべて再現した。**

```
[min_ca=0]      RAISED LinAlgError: 0-dimensional array given
[NaN rmsd]      accepted=True  fit_rmsd=39.146      <- 39 A の fit を通す
[collinear]     rmsd=0  det=1.000                    <- 軸回りの回転が不定
[multi-model]   残基は model 1、CA 座標は model 2 (z=99)
[altLoc]        occupancy 0.70 が z=0 なのに z=50 を採用
```

### 手続き上の問題も 1 件

レビュー依頼には「ファイルを一切変更しないこと」と明記したが、cursor は
`tests/test_membrane_orientation.py` にテストを 1 本追加していた
(`test_partial_outage_with_a_completed_no_match_is_not_total_unavailability`、
07:05:36)。他ファイルへの混入はない (memo と tool-reference の変更は私のもの)。
**ただし指摘内容は正しかった**: 1 鎖が HTTP 500、別の鎖が正常に 0 件だったとき、
私の実装は `opm_homolog_search_unavailable` (「どの鎖も検索できなかった」) を返していた。
実際には 1 鎖は検索できて「該当なし」と答えている。アサーションは実在しない文言を
要求していたので書き直し、コード側を「検索できた鎖の結果と、検索できなかった鎖の数を
両方述べる `opm_homolog_no_match`」に修正した。

### 直したもの

| # | 内容 |
|---|---|
| P1 | **膜サブセットの identity をゲート追加**。全鎖 identity だけだと、大きな可溶性ドメインを共有し膜ドメインが無関係な donor が通る。fit は膜サブセットで行うのだから、対応が実在すべきはそのサブセット |
| P1 | **公開パラメータの検証** (`opm_homolog_gates_invalid`)。範囲外・非有限を拒否し、**fallback ではなく失敗**にする。ゲートを黙って緩めるのは依頼を断るより悪い |
| P2 | **同一配列 query chain は検索だけ共有し、フィットは物理鎖ごと**に行う |
| P2 | **全候補を評価してから選ぶ** (ユーザ判断)。RCSB の順位は検索関連度であって配向品質ではない |
| P2 | **縮退フィットの棄却** (`opm_min_fit_condition`)。共線 CA は RMSD 0・det 1.0 で通るが軸回りの回転が任意 |
| P2 | **DL 失敗をゲート不合格と分離** (`opm_homolog_fetch_unavailable`)。判定していない donor を「品質不足」と報告していた |
| P2 | **model 1 と最高 occupancy altLoc だけ**を fit にも出力にも使う |
| P2 | **総時間予算** (`opm_total_budget_seconds`、既定 600 s)。従来は 120 s × 鎖数 × 候補数 |
| P3 | **キャッシュのアトミック書き込みと整合検査**、SHA-256 記録 |
| P3 | **空ボディの 200 は unavailable**。204 だけが RCSB の no-hit |

### 縮退しきい値は実測で決めた

`s3/s1` (最小/最大主成分ひろがり): 実膜 CA 集合 **0.685-0.702**、単一の理想 α ヘリックス
40 残基 **0.093**、共線・共面 **0.000**。**0.01** なら両側に一桁の余裕がある。
なお**テスト fixture 自体が縮退していた** (`_membrane_path` の x と y が比例 = 平面曲線)。
新ゲートがそれを正しく検出したので、fixture を真に 3 次元の螺旋に書き直した。

### 全候補評価にしたら、ランキングの欠陥が実データで出た

ライブ実行で 10 候補すべてを採点したところ:

| pdb | 膜内identity | 膜内CA | fit RMSD |
|---|---|---|---|
| 5l7i | 1.000 | 201 | 0.325 A |
| **5l7d** | 1.000 | **200** | **0.000 A** |
| 7zi0 | 1.000 | 197 | 0.181 A |
| 6ot0 | 0.994 | 176 | 1.927 A |

当初の順序 (identity → 膜内 CA 数 → RMSD) は、**CA 数 201 対 200 の 1 残基差で
5L7D 自身 (完全一致、RMSD 0.000) を 5L7I に負けさせた**。0.5% の支持量差が 3 倍の
RMSD 差を上書きするのは誤り。支持量を**最良候補比の 1/10 刻み**に丸め、同程度なら
RMSD で決めるよう修正した。

修正後は **5L7D 自身が選ばれ、PDBTM 法線誤差 5.9 度**。これは
**OPM と PDBTM という 2 つの参照 DB の不一致そのもの**であり、転写手法の理論的下限。
query が OPM に登録済みという有利なケースではあるが、パイプラインが端から端まで
正しく動いていることの証明にはなる。

前エントリの 6.3 度 (6OT0) と 11.8 度 (4JKV) も同じ 5L7D に対する値で、
**どの donor を選ぶかで 5.9-11.8 度動く**。donor 選択がこの手法の精度を支配しており、
fit RMSD ではないことが改めて確認された。

### 評価された点 (churn するなと明記された)

CIGAR walk の I/D 方向は両向き確認して正しい、DUM スラブ限定は全体 fit や
trimming より明確に安全、donor 鎖の gate 優先、部分障害で後続鎖を止めない、
JSON の鎖別棄却理由。

### テスト

ruff clean。`test_membrane_orientation.py` は 26 → **62** 本。
contract 系込みで 454 passed。

---

## 2026-08-19 — 訂正: 「OPM 相同体転写は明確に悪い (13.5 度)」は誤り。実検索の donor では 6.3 度

ユーザに「全鎖 HTTP 500 はおかしい」と指摘されて RCSB を直接叩いたところ、**検索は正常に
動いていた**。切り分けの過程で 2 件の実バグが出て、さらに**前 2 エントリの精度評価が
覆った**。

### バグ1 (重大): ヒット 0 件を「通信障害」と誤報告していた

RCSB は結果 0 件を **204 No Content + 空ボディ**で返す。urllib は 2xx を成功として扱うので
`HTTPError` は上がらず、`json.load` が JSONDecodeError で落ち、汎用ハンドラが
`"RCSB search unavailable: JSONDecodeError"` を返していた。つまり
**「この鎖には OPM 相同体が無い」が「検索サービスに到達できない」に化けていた**。
多鎖集約と噛み合うと、OPM 相同体を持たない蛋白が全鎖 no_match のはずが
`opm_homolog_search_unavailable` として報告される。`response.status == 204` と空ボディを
明示的に「該当なし」として扱うよう修正。

### バグ2: HTTPError の本文を捨てていた

`f"RCSB search returned HTTP {exc.code}"` だけを返しており、500 が本当のサーバ障害なのか
クエリ不正なのか区別できなかった (実際それで診断が止まった)。本文 300 字を添えるよう修正。

なお **8/18 に観測した HTTP 500 は本物のサーバ側障害**で、8/19 時点では解消している
(SMO 配列 + OPM フィルタで 3.2 秒 / 19 件)。albumin + OPM は 204 = 0 件が正解。

### バグ3: 検索値のスケールが混在していた

RCSB の `match_context.sequence_identity` は **0-100 のパーセント** (95.5)。これを
`local_identity` (0-1、0.81 等) と同じ JSON に並べて記録していた。`query_coverage` は
**そもそも返ってこない** (`query_beg/query_end/query_length` はある)。identity を分数に
正規化し、coverage は範囲から導出、`search_alignment_length` も記録するようにした。
いずれも provenance のみでゲートには使わない方針は不変。

### 訂正: 転写の精度評価は「手で選んだ donor」の評価だった

前 2 エントリは **転写 13.5 度 (後に膜スラブ限定で 11.8 度) > PPM3 6.8 度**、
「転写は明確に悪い」「カスケードの主経路にはしない」と書いた。**これを取り消す。**

その測定はすべて donor を **私が手で 4JKV に固定**して行ったもので、検索が実際に返す
donor で測っていなかった。バグ1を直して**完全ライブ (mock 一切なし)** で通したところ:

| donor | 選定 | 膜内 CA | fit RMSD | PDBTM 法線誤差 |
|---|---|---|---|---|
| 4JKV | 手で指定 | 193 | 0.81 A | 11.8 度 |
| **6OT0** | **実検索の最上位** | 176 | 1.93 A | **6.3 度** |

6OT0 (SMO の cryo-EM 構造) からの転写は **6.3 度**で、**PPM3 の 6.8 度より良く**、
参照 DB 同士の不一致 5.9 度の内側にある。つまり「転写だけが 5.9 度の外側」という
前エントリの主張は成立しない。

**fit RMSD は法線誤差を予測しない。** 4JKV は 0.81 A で 11.8 度、6OT0 は 1.93 A で 6.3 度と
逆順になる。重ね合わせの残差はドナー座標との一致度であって、ドナーの膜フレームが
どれだけ正しいかとは別物である。ゲートは「どこまで悪い donor を許すか」の下限であって
donor の順位付けではない、と読むべき。

### donor 側 gate 優先選択の実戦での効き

6OT0 は 6 鎖ある。全 gate 適用後に残ったのは受容体鎖 R のみ:

| donor chain | identity | coverage | 膜内 CA | 判定 |
|---|---|---|---|---|
| R | 0.997 | 0.722 | 176 | **採用** (RMSD 1.93 A) |
| A | 0.298 | 0.741 | 2 | identity 不合格 |
| B | 0.324 | 0.707 | 1 | identity 不合格 |
| G | 0.793 | 0.122 | 6 | coverage 不合格 |
| L | 0.644 | 0.219 | 0 | coverage 不合格 |
| H | 0.579 | 0.265 | 0 | coverage 不合格 |

query 側も albumin 578 残基 (longest) が `no_match`、SMO 475 残基で採用。所要 4.1 秒。

### 残る留保

6.3 度は 5L7D 一例の値であり、6OT0 が同一蛋白のほぼ完全一致 (identity 0.997) である
有利なケース。遠縁の donor で同じ精度が出る保証はない。**手法の順位を主張するには
複数ターゲットでの測定が要る**。今回言えるのは「前エントリの『転写は明確に悪い』は
donor 選定の人為で、実検索経路では成立しない」ことまで。

---

## 2026-08-19 — OPM 転写を「全 protein chain を検索」「gate 通過鎖の中から最良」に修正

差分レビューで受入れ前の必須修正として 2 点指摘された。どちらも**主経路が使えるはずの
構造で黙って使われなくなる**類の欠陥で、fallback が働くので失敗としては表面化しない。

### 1. query 側: longest chain しか検索していなかった

膜蛋白の複合体は「長い可溶性パートナー + 短い膜サブユニット」がごく普通の形で、
その場合 OPM 相同体を持つのは短い方だけ。longest chain だけを検索すると、**主経路が
存在するのに no_match で PPM3 に落ちる**。修正後は全 protein chain を長い順に検索し、
最初に全 gate を通った donor で確定する。同一配列の鎖 (ホモ多量体) は 1 回だけ検索する。

`opm_homolog_search.json` は per-query-chain 構造に変更した:
`query_chains[*]` に chain / equivalent_chains / residues / outcome
(`accepted` | `rejected` | `no_match` | `search_error` | `not_searched`) /
search_error / candidates を鎖ごとに分けて記録する。

**ある鎖の通信エラーで全体を打ち切らない。** 1 鎖の HTTP 500 は他鎖について何も語らないし、
相同体を持つのは往々にして後の鎖である。全鎖が通信不能だったときだけ
`opm_homolog_search_unavailable` を返し、reason に鎖ごとのエラーを列挙する。
候補が 1 つでも評価されていれば `opm_homolog_rejected`、どの鎖もヒット無しなら
`opm_homolog_no_match`。OPM 構造の取得・パースは PDB ID ごとに 1 回だけで、
複数の query chain が同じ entry に当たってもキャッシュを再利用する。

### 2. donor 側: 最低 RMSD を先に best にしてから gate を掛けていた

旧実装は donor の全鎖のうち fit RMSD が最小の鎖を best とし、**その後で** identity /
coverage を判定していた。短い無関係な区間にアラインした鎖は「短いからこそ」タイトに
重なるので、本当の対応鎖を押し退けて candidate 全体を巻き添えで棄却させ得る。
修正後は `_fit_donor_chain` が鎖ごとに全 gate を適用し、**全 gate を通った鎖の中から
最低 RMSD** を選ぶ。全鎖不合格なら各鎖の数値 (identity / coverage / membrane CA / RMSD) と
rejection reason を `homolog_chains` に残し、gate を最も先まで通った鎖の理由を
candidate の rejected に採用する。

合成 donor で実証: 鎖 P (identity 1.00, memCA 80, RMSD 0.45) と鎖 Q (identity 0.36,
memCA 80, RMSD 0.00)。旧規則は Q を選んで identity で候補ごと棄却、新規則は P を採用する。

### 実測 (5L7D)

アルブミン 1AO6 chain A (578 残基, 可溶性) を chain A、5L7D chain A (475 残基) を
chain B とした実構造 2 鎖複合体で検証。longest chain は可溶性側になる。

| ケース | 結果 |
|---|---|
| 両鎖に 4JKV を提示 | chain A は identity 0.298 で棄却 → chain B で採用 |
| chain A だけ HTTP 500 | chain B で採用 (打ち切られない) |
| 全鎖 HTTP 500 | `opm_homolog_search_unavailable`、両鎖のエラーを列挙 |

採用時の数値は donor chain B / aligned 432 CA / 膜内 193 CA / fit RMSD 0.808 A /
厚さ 31.9 A、**PDBTM 法線誤差 11.8 度で変更前と完全に一致**。donor は 4jkv を 1 回だけ取得。
donor 側は chain A (RMSD 0.813) と chain B (0.808) がともに全 gate を通り、低い方の B を選ぶ。

なお **RCSB の sequence 検索は今日も HTTP 500 のまま**で、実通信では両鎖とも
search_error になり PPM3 に落ちる。主経路がオンライン依存である点は前エントリのとおり。

### 変更ファイル

`mdclaw/solvation/opm_orient.py` (`_fit_donor_chain` / `_consider_candidate` を新設)、
`mdclaw/guardrail_codes.py` (説明文のみ、code は不変)、`tests/test_membrane_orientation.py`
(33 tests)、`docs/developer/tool-reference.md`、`skills/md-prepare/membrane.md`。
tool-reference に残っていた "outlier-trimmed Kabsch fit" の古い記述も膜スラブ限定に直した。

---

## 2026-08-18 — 方針転換: TMbed を全廃し、OPM 相同体転写 → PPM3 のカスケードへ

承認された計画 (`~/.cursor/plans/opm-ppm-orientation-f89b45ec.plan.md`) に沿って実装。
配向は「OPM 相同体があれば転写、無ければ PPM3」になり、TMbed と ProtT5 はコード・CLI・
依存・コンテナ資産から完全に削除した。**過去エントリの測定と結論は取り消していない。**

### 実装したもの

- `mdclaw/solvation/opm_orient.py` (新規)。入力鎖配列 → RCSB Search API の sequence 検索と
  `rcsb_polymer_entity_annotation.type=OPM` の積集合 → OPM 公開 PDB 取得 → gemmi 配列
  アラインメント → 外れ値除去つき Kabsch → 入力構造全体 (リガンド含む) へ適用 → DUM から
  膜中心を読んで z=0 に揃える。品質ゲート (identity / coverage / 対応CA数 / fit RMSD) を
  引数化し、**不合格候補も理由と数値を `opm_homolog_search.json` に残す**。
- `membrane.py` の `auto` を OPM→PPM3 に変更。`_orient_for_membrane` が試行履歴
  (backend / success / code / reason) を `result["orientation"]["attempts"]` に記録する。
  MEMEMBED と PPM は明示指定として残す。`tm-segments`、`membrane_topology_file`、
  `auto_predict_topology`、TMbed 由来の barrel 判定と topology consistency は削除。
- `ppm_orient.py` の「n_terminal_side 未指定を黙って out にする」挙動を廃止。PPM3 は値を
  必ず要求するので PPM 自身の慣習で走らせるが、**assumed であることを warning と
  `n_terminal_side_assumed` に明記**する。

### 実装中に判明したこと

**RCSB の sequence 検索は現在サーバ側で継続的に失敗する** (HTTP 500、"did not complete
ticketId within 30000 ms"、5 回連続)。OPM annotation フィルタ単体は動く (18,981 entity)。
つまり主経路がオンライン依存で、現に落ちている。計画どおり通信失敗は失敗コードではなく
fallback event として扱うので実害は出ないが、**本番でどれだけ転写が使われるかは RCSB の
状態次第**であることは記録しておく。

**全対応ペアで一括 Kabsch すると実用にならない。** 5L7D (CRD あり) に 4JKV (7TM のみ) を
当てると 429 対応ペアで fit RMSD 14.84 A となり品質ゲートで棄却された。当初は外れ値の
反復除去 (中央値ベース) で対処したが、**レビュー指摘を受けて廃止した**。trimming は
「最もよく合う部分集合」を選ぶので、二つの蛋白が大きな可溶性ドメインを共有しつつ膜内の
座り方が違う場合、**そのドメインだけで膜配向を決めてしまう**。膜転写がやってはいけない
ことそのものだった。

現在は **donor 自身の DUM z 範囲 (±2 A マージン) 内にある対応残基だけで Kabsch** する。
品質ゲートは (a) 全配列のローカル identity/coverage、(b) 膜スラブ内の対応 CA 数、
(c) そのフィットの RMSD の三本立て。`_kabsch_trimmed` は膜が絡まない比較用の補助関数として
残すが主経路からは外した。

**膜スラブ限定にすると法線誤差が 13.7 → 11.8 度に改善した** (独立参照 PDBTM 比)。
5L7D→4JKV で膜内対応 193 CA / fit RMSD 0.81 A。過去に素朴な残基番号一致で測った
162 CA / 0.60 A と整合する (±2 A マージンのぶん残基が多く RMSD も僅かに大きい)。
ただし依然として PPM3 の 6.8 度より悪く、参照系どうしの不一致 5.9 度の外側にある。
**これは重ね合わせの粗さではなく手法固有の値**で、PPM が構造ごとに独立に最適化するため
同一蛋白の別構造でも OPM 注釈が約 5 度食い違うことに由来する。

**RCSB 検索は POST + `results_verbosity=verbose` に変更した。** 膜蛋白の配列は URL
クエリに収まらない。また RCSB の `match_context` は `sequence_identity`/`query_coverage`
を欠くことがあり、None のままではゲートを素通りする。identity と coverage は gemmi
アラインメントから**必ずローカルに算出**し、検索側の値は provenance にのみ残す。

**OPM の URL は MoleculeKit 実装と同じ `https://storage.googleapis.com/opm-assets/pdb/{id}.pdb`
に統一。** キャッシュ判定に掛けていた 5000 バイト下限も除去した (小さい構造が永久に
再ダウンロードされる)。サイズ検査はダウンロード直後のみ。

### 記録しておく懸念

計画は転写を主経路に据えているが、私が独立参照 (PDBTM) で測った限りでは
**転写 13.7 度 > PPM3 6.8 度 > TMbed 7.8 度 > MEMEMBED 8.8 度** で、転写が最も悪い。
参照系どうしの不一致 5.9 度の中に他 3 手法は収まるが、転写だけ外側にある。
「OPM 標準への準拠」を目的とするなら転写は定義上正しい選択であり、その前提なら妥当。
物理的な正確さを目的とするなら、この順位は再検討の材料になる。

---

## 2026-08-18 — 訂正: 配向手法の精度比較は循環していた。PPM3 は同梱バイナリが壊れている

前エントリまでで「PPM3 が 1.0 度で最も正確」「MEMEMBED は大きな可溶性ドメインで裏返る」と
書いたが、**どちらもユーザの指摘と実測で否定された**。

### 訂正1: 精度比較の基準が循環していた

「何と比較して精度を求めているのか」と問われて気づいた。全測定を **OPM の 5l7d エントリを
正解として**行っていたが、**OPM のエントリは PPM が生成したもの**である。PPM を OPM に対して
測れば一致するのは当然で、1.0 度は精度ではなく自己一致にすぎない。さらに TMbed も論文 p.3 で
「OPM の ATOM 座標から inside/outside ラベルを割り当てた」とあり訓練ラベルが OPM 由来。

独立参照として **PDBTM (TMDET アルゴリズム、PPM とは別手法)** を取得して測り直した結果:

| 手法 | OPM 基準 (循環) | PDBTM 基準 (独立) |
|---|---|---|
| OPM/PPM そのもの | 0 (定義上) | **5.9 度** |
| PPM3 (パッチ後) | 1.0 度 | 6.8 度 |
| tm-segments (TMbed) | 6.4 度 | 7.8 度 |
| MEMEMBED | 5.5 度 | 8.8 度 |
| OPM 相同体から転写 (4JKV 同一蛋白) | 5.2 度 | 13.5 度 |

**参照系どうしが 5.9 度食い違っており、転写を除く全手法がその不確かさの中に収まる。**
つまりこの測定では PPM3 / TMbed / MEMEMBED の優劣を主張できない。主張できるのは
「OPM 相同体からの転写 (13.5 度) は明確に悪い」ことだけ。同一蛋白・重ね合わせ RMSD 0.60 A
でも 13.5 度ずれるのは、転写行列の誤差が上乗せされるため。カスケードの主経路にはしない。

### 訂正2: MEMEMBED は 5L7D で裏返らなかった

「SMO の大きな CRD が MEMEMBED の統計ポテンシャルを引っくり返す」と繰り返し書いたが、
結晶座標から素で走らせたら **正しい向き** (CRD が +56.4、H8 が -17.7、法線誤差 5.5 度)。
別メンバーの系が裏返ったと私が推測した根拠は報告値の Z 範囲だけで、実物は見ていない。
彼らが使ったのは MEMEMBED ではなく自前ビルドの PPM3 で、構造も AlphaFold モデルだった。
**「MEMEMBED は大きな可溶性ドメインで裏返る」は実証されていない。**

### MEMEMBED -f は不採用 (実測で悪化)

TMbed が非膜と判定した 336 残基を `-f` でスコアから除外したところ、法線誤差が
**5.5 度 → 25.7 度に悪化**。膜外残基はノイズではなく信号だった。`mempot[20][34]` は
「親水性残基が端のビンにいること」自体をスコアにしており、除くと膜貫通部 160 残基で
34 ビンを埋めることになり拘束が足りない。positive-inside rule も膜外の Arg/Lys 分布に依存する。
なお `-f` の挙動自体はソースで確認済み: `parse_pdb` が backbone 配列に加えないだけで、
出力 PDB からは消えない (3754 原子で不変)。

### PPM3 の同梱バイナリは壊れている

`immers` を叩くと解析は完走するのに、結果を出力する直前で Fortran ランタイムエラー。
`opm.f:485` の FORMAT 記述子にカンマが欠落している (`f7.0''+-''`)。旧い gfortran は許容、
現行は実行時に拒否。**出力 PDB が一切生成されない。** カンマ 1 個を足して再ビルドすると
完全に動く (膜厚 30.4 A も出力)。Dockerfile の openmm-builder ステージで gfortran を入れ、
パッチして再ビルドし `/opt/mdclaw/bin/immers` を差し替えるようにした。
別メンバーが「PPM3 成功、tilt 19.4 度、thickness 29 A」と報告していたのは
**まさにこのクラッシュする行が出す値**で、彼らのビルドは許容する gfortran だったのだろう。

### PPM3 に渡せるトポロジー情報は itopo (in/out) の 1 ビットのみ

`opm.f:84-123` の stdin 入力は 8 項目で全部 (inptype / keepligs / pdb / 膜の数 / 膜タイプ /
曲率 / itopo / 鎖リスト)。セグメントを渡す入力は存在しない。TMbed の `n_terminal_side` を
7 番目の itopo に渡す形で実装済み。

### PPM バックエンド追加の根拠

精度を根拠にはできなくなったので、残る論拠は機能差:
- **膜厚を推定する** (30.4 A)。MEMEMBED は `pdb.cpp:299-300` で ±17.5 A 固定
- **決定論的** (MEMEMBED は GA。ただし散らばりは未測定)
- 独立な第 3 の意見として食い違いを検出できる

### barrel の文字列マッチを廃止

`_infer_beta_barrel_from_context()` を関数ごと削除。study 文書やパスに "beta barrel" が
含まれるかを見る判定で、否定文を区別しないため「beta barrel は対象外」と書いただけで
barrel 扱いになっていた。判定は TMbed の H/B クラスのみに一本化。

### 未解決

- KcsA 単量体 (TM 2本) で法線誤差 29.1 度。四量体 (8本) なら 0.1 度。**セグメント数が
  少ないと壊れる**が、`MIN_SEGMENTS_FOR_AXIS = 1` は緩すぎる。ガードが要る。
  なお re-entrant loop が深さを壊すという仮説は否定された (中心面は KcsA/aquaporin とも
  +1.4 A で安定)。壊れるのは法線の方。
- TM 予測と配向を DAG ステージとして分離する件 (現状は embed 内で毎回 TMbed を実行)
- MEMEMBED の mempot が何から導出されたかは未確認 (OPM 由来なら MEMEMBED も循環に含まれる)

---

## 2026-08-18 — レビューを受けた膜配向の修正。「回転不変」という私の主張は誤りだった

pane の cursor agent (GPT-5.6) に db7d509 をレビューさせたところ、P1 が 3 件出た。
特に痛かったのは **power iteration の初期ベクトル固定**で、`_principal_axis` が
`v0 = [1,1,1]` から始めるため、真の第1固有ベクトルがそれと直交すると成分がゼロのまま
収束しない。レビュアーが実際の 20 残基ヘリックスを回転させて再現し **90.0 度ずれる**ことを
示した。私のランダム回転5回のテストが通っていたのは、厳密な直交が測度ゼロだから運が
良かっただけで、**「任意の開始フレームから同一」という docstring の主張は誤りだった**。
`numpy.linalg.eigh` に置換して 0.00 度。あわせて λ2/λ1 の縮退検査を入れ、方向の定まらない
点群 (球状、短すぎるセグメント) には `None` を返すようにした。5L7D の実測 6.4 度は不変。

**配向がパッキングの内部ステップだった**のも P1。`orient_fn` は
`embed_with_membrane_patch_tiles` のステップ1 からしか呼ばれておらず、
`--membrane-backend packmol-memgen` を選ぶと配向指定が全部無視され packmol-memgen 内部の
MEMEMBED が走っていた。ユーザから「配向の話になぜ patch が出てくるのか」と指摘され、
症状ではなく構造が問題だと整理できた。配向を `embed_in_membrane` の前段へ引き上げ、
両パッキング経路とも `preoriented` で配向済み構造を受け取る形にした。レビュアーの案の方が
私の当初案 (packmol-memgen 内部の MEMEMBED にフラグ注入) より良い。

**最大の設計ミスは別にあった。** ユーザに「膜トポロジーはどこで使っているのか」「TMbed は
どこで使っているのか」と繰り返し問われて判明したが、`embed_in_membrane` は TMbed を呼んで
おらず、`--membrane-topology-file` を渡し忘れると**黙って MEMEMBED 経路に落ちていた**。
膜系を作る以上トポロジーは必須の入力なのに、任意のオプションとして扱っていた。これは
「MDClaw が MEMEMBED に `-n` を渡していなかったから SMO が裏返った」のと同じ構図で、
必要な情報をコードが取りに行っていなかった。既定を `auto_predict_topology=True` に変え、
トポロジーが無ければ自分で TMbed を実行するようにした。予測不能なら従来どおり MEMEMBED に
落ちるが、**必ず warnings に理由を残す** (黙って落ちるのが問題だったため)。

P2 は 4 件。(1) topology consistency が生の z を膜中心と比較しており、周期境界を跨いだ残基が
反対側と判定されていた → 最小イメージ化。(2) 残基キーが resseq のみで chain を無視しており、
残基番号を共有するホモ多量体で両 protomer が平均されて整合率 0.5 になっていた →
(chain, resseq, icode) に。膜蛋白では多量体が普通なので実害が大きい。(3) TMbed の
subprocess に timeout が無く、存在しない model_dir は黙って HuggingFace ダウンロードへ
フォールバックしていた → timeout 1800s と `tmbed_model_dir_missing`。(4) 新パラメータが
`actual_conditions` に無く、条件を正しく記録した DAG ほど `condition_missing` で実行不能に
なっていた → 追加。トポロジーは可変な絶対パスではなく**内容の SHA-256** を記録する
(レビュアーの提案。パスは環境で変わるがハッシュなら同一性を検証できる)。

**レビュアーの数値に合わせなかった点が1つある。** PBC の再現例として提示された入力
(headgroup 20..80 に対し out=30 / in=70) は、膜中心 50 に対して out が下・in が上という
自己矛盾で、0.0 が返るのが正しい。物理的に意味のある「残基が周期境界を跨ぐ」ケースで
検証し直し、3 パターンとも 1.0 になることを確認した。

**beta barrel の扱いも確定。** テンソル和 Σaaᵀ がレビュアー環境では barrel を 0.0-2.2 度に
改善したが、私の手元では 2OMF 17.3 度 (符号合わせ平均 14.5 度より悪化) / 4K3B 11.7 度
(同 31.1 度より改善) と一貫しなかった。セグメントを全部そろえられるかに強く依存すると
見ている。決着まで barrel は MEMEMBED `-b` に回す現状維持。判定は TMbed の H/B クラスで、
7AHL (strand 2本) / 1UUN MspA (3本) / 4K3B BamA (16本) を含む実バレル5件すべてが拒否され、
SMO (helix 7本) のみ通ることを確認済み。論文が「取り逃すのは 2-4 ストランドのもの」と
書いていたので穴だと推測したが、実測で否定された。

実系確認: `--membrane-topology-file` を渡さず1コマンドで solv_006 を構築し、
orientation_method=tm-segments、membrane_center_z=0.0、geometry passed、
topology_consistency 10/10。回帰テスト 33 本、558 passed。

**未着手**: PPM バックエンド (`/opt/mdclaw/bin/immers` は SIF に既存)、MEMEMBED `-f` への
非膜残基受け渡し、study 文書の文字列マッチ由来 barrel フラグより TMbed 判定を優先する件。
また packmol-memgen 経路はユニットテストと構造変更で確認しただけで、フルボックス packing を
実走させていない。

---

## 2026-08-18 — 訂正: DB 由来ベンチ設計を MDDB 単独に変更、逐次ゲートと σ_FF 加算式を撤回

**同日の前エントリ「公開 MD DB (GPCRmd / MDDB) 由来ベンチの実測と検証層の設計」を訂正する。**
実測値そのものは概ね維持されるが、**供給源の選択と検証設計の中核 3 点が誤っていた**。
改訂版は `docs/research/db_derived_benchmark_validation.md` (rev.2)。

**方針変更 (ユーザ判断).** GPCRmd は RIKEN でのライセンス上の扱いが難しいため供給源から外した。**MDDB 単独**にする。

**独立レビューで判明した設計上の誤り 4 点** (cursor advisor pane, Opus 4.8, 読み取り専用で実施):

1. **`observable_fidelity` を「唯一の新規軸」としたのは誤り。** 軌道から観測量を再計算して
   自己申告値と突き合わせる primitive は既存: `MDPrepBench/mdprepbench/scoring.py:882-947` と
   `:1076-1124` (`_check_observable_recompute_consistency`)、
   `MDStudyBench/mdstudybench/scoring.py:1033-1075` (`direction_grounding`) と
   `:1078-1126` (`observable_recompute_consistency`)。新規なのは
   **DB の固定参照軌道をエージェント入力にする task mode と DB provenance 付き check contract** だけ。
2. **「軸 k は k-1 が通ったときのみ評価」という逐次ゲートは自己矛盾。** 物理妥当性に落ちた提出でも
   組成・自己申告値・主張整合性の診断は独立に可能で、それを捨てるのは掲げた目的 (原因帰属) を捨てること。
   **全軸を独立に評価し、`passed` / `failed` / `not_evaluable` / `not_attempted` を区別し、
   最終合否だけを非補償ゲートにする**に変更。
3. **`δ = k·sqrt(σ_rep² + σ_FF²)` を撤回。** この式は力場差が平均ゼロのランダム変動で、
   単一 σ_FF が系・観測量をまたいで転用可能であることを仮定する。実際は系依存の系統バイアスなので
   単一分散に畳めない。同様に「Δ なら力場オフセットが相殺される」も一般には成立しない
   (相殺は bias が両条件で同じ場合のみ)。
4. **旧 L4 を 2 軸に分割。** `observable_recompute` (selection/alignment/PBC/実装版の問題) と
   `ensemble_reproduction` (sampling/力場/初期条件/protocol の問題) は失敗原因が異なる。
   後者は matched-protocol / diagnostic-only / calibrated の 3 モードに分け、
   matched-protocol なら σ_FF は不要 (「σ_FF が測れなければ絶対値タスクを一切作らない」は強すぎた)。

**新規に見つかった scorer バグ.** `DeterministicCheck.capability` の明示 override
(`MDPrepBench/mdprepbench/models.py:291-306`) が capability profile 集計で無視される。
`CheckResult` (`models.py:1079-1085`) が capability を保持せず、`scoring.py:3662-3685` が常に
`DEFAULT_CHECK_CAPABILITY` を引くため。現行 P01-P40 は override 未使用なので今の得点には影響しないが、
自動生成タスクが capability を明示し始めると公開契約と実際の集計が食い違う。**タスク量産前に修正が必要。**

**MDDB 単独 + CC-BY 限定にした結果の実測 (定義つき).**

- ライセンス: CC-BY 4.0 が 4511、CC0 19、**CC 系でないものが 24** (AFL 3.0 が 9、Apache 2.0 が 5、
  MIT 4、LGPL 2、記載なし 4)。タスク生成はこの 24 件を除外する。
- **膜系の軸は実質失われた。** 実バイアレイヤ (`LIPIRES>=100`) は 30 件だが **20 件が非 CC**
  (CLC / Nav 5WEO / TARP / HCN / CTL1、および唯一の GPCR `OTRMG` `OTRMGb` も非 CC)。
  CC-BY の膜系は 10 件で全て SARS-CoV-2 のウイルス膜。
  **P18 膜系が全モデル失敗する既知の弱点を DB 由来タスクで補強する道は閉じた。** 膜系は手書きで扱う。
- **力場感度の測定源は MDDB 内に存在する。** 同一 PDB が複数力場で登録された群が **11、全て CC-BY**。
  `6VXX` が 6 力場、`6M0J` が 5、`1FZX` / `1ICK` / `1SK5` / `3GGI` が 4
  (OL15 / OL21 / ParmBSC1 / Tumuc1、各 2 entry) で、核酸 4 系は力場比較目的の study に見える。
  ただし同一 PDB でもリガンドパラメータ・プロトネーション・欠損ループ・イオン強度・ensemble・
  engine・軌道長・初期構造が交絡しうるため、**matched を確認するまで力場感度に帰属しない**。

**計数の定義の問題.** 前エントリの「脂質を含む 43 件」は定義なしで誤読を招く。
`LIPIRES>0` は 43、`LIPIRES>=100` は 30、`MEMBRANES` 非空は 10 で、どれを指すかで意味が変わる。
また `totalFrames` 296128391 は summary エンドポイントの集計値で、project 一覧の総和 287267536 とは
別の量である (3% 差)。**以後、計数は必ず定義とともに記す。**

**次の 4 手 (いずれも MD 不要).** (1) 核酸 4 系 16 entry の matched-protocol 検証、
(2) 解析契約レジストリの最小版 (観測量 1 つで MDDB 前計算値と自前再計算値のずれを測る)、
(3) `observable_recompute` タスク 10 本、(4) 上記 scorer バグの修正と回帰テスト。

---

## 2026-08-18 — 公開 MD DB (GPCRmd / MDDB) 由来ベンチの実測と検証層の設計

MDPrepBench / MDStudyBench を公開 MD データベースから自動生成できるかの調査。
設計は `docs/research/db_derived_benchmark_validation.md` に分離。ここには実測値と判断だけ残す。

**実測 (API / 公開ページを直接叩いた).**

- MDDB (`https://mmb.mddbr.eu/api/rest/v1/`, 無認証): 4554 projects / 14138 MD /
  296M frames / 33.6 TB。`LICENSE` は 4511 件が CC-BY 4.0。条件ベクトル
  (`FF` `TEMP` `WAT` `ENSEMBLE` `TIMESTEP` `LENGTH` `SOL` `NA` `CL` `MEMBRANES` `PDBIDS`) が機械可読。
  前計算解析が約 4500 系 × 10 種 (`rmsds` 4551 / `fluctuation` 4551 / `rgyr` 4551 / `sasa` 4552 /
  `pca` 4552 / `tmscores` 3285 / `hbonds` 2318 / `interactions` 2398、膜系は `apl` `thickness`
  `lipid-order` `mem-map`) で JSON 時系列としてそのまま取得できる。`mdcount>=2` が 1328 project
  (10 replicas が 605、6 が 271、8 が 160、9 が 152)。
- **MDDB に GPCR はほぼ無い。** 全 4554 中で脂質を含むのは 43 件のみ、うち GPCR は
  `OTRMG` / `OTRMGb` (ヒトオキシトシン受容体, 7RYC, Amber ff14SB, 3 replicas, LIPIRES=256) の 1 系だけ。
  残りは CLC (8-9 replicas)、Nav (5WEO)、TARP γ2/γ7、HCN、CTL1、SARS-CoV-2 spike/膜、
  および LIPIRES=1 の界面活性剤単分子系。膜系タスクで MDDB は GPCRmd の代替にならない。
- GPCRmd: API とファイル DL はログイン必須 (DL は 1 リクエスト 5 dynamics 上限) だが、
  **`/dynadb/dynamics/id/<id>/` の report ページは無認証で完全な条件表を返す**。
  ID 36 実測: 3REY.A / Inactive / TIP3P / POPC / Cl 191 mM, Na 159 mM /
  Water 22376, POPC 207, Cl 77, Na 64 / 100039 atoms / CHARMM36m / 4.0 fs / Replicates 3 / 1.5 µs。
  `/dynadb/datasets/` は無認証で 773 の view ID を Complex / Apoform ペアとして公開。
- **GPCRmd は CHARMM 一様ではない。** 実在 24 ID をサンプルして 12 件パースできたうち、
  1 件が ff19SB/lipid21/GAFF2 + AMBER PMEMD.CUDA (ID 2322)。CHARMM も 36 / 36m Feb2016 /
  May2015 / c36 Jul2021 と版が割れ、エンジンは ACEMD / ACEMD3 / GROMACS 2021.3 / PMEMD、
  膜は POPC 単一が 6 件で残り 4 件が混合 (DOPC/DPPC/DSPC/SDPC、POPC+CHL1、POPG+CO1+POPC)。
  Nature Methods 2020 のコアが一様なだけで、以後のコミュニティ投稿は多様化している。
- 24 ID 中 12 は report ページを取得できず (500 / no report)。773 は上限であって使える N ではない。
  8/24-8/28 は GPCRmd メンテナンス予定。

**既存ハーネスの状態 (grep で確認).**

- MDPrepBench: `check_type` は 24 種すべて**バージョン無し**。集計は重み付き平均 (補償的) +
  `_HARD_FAIL_CHECK_TYPES` クランプ。軸は `identity` / `physical_validity` / `fidelity` / `provenance`。
- MDStudyBench: `region_water_occupancy@1` 形式で**バージョン有り**。
  `grounded_correct = valid_execution AND claim_supported AND truth_agreement` の非補償 AND。
- **どちらにも solve 時のネットワーク遮断が無い** (`run.py` に該当制御なし)。
  参照 DB を使うベンチではこれが最大の穴で、エージェントが参照そのものを取得できると全層が同時に無効化される。

**判断.**

- 検証は 7 層 (`identity` / `physical_validity` / `composition_fidelity` / `execution_validity` /
  `observable_fidelity` / `claim_support` / `truth_agreement`) に分け、層をまたぐ補償はしない。
  外部 DB が要るのは 3 層だけで、残り 4 層は DB なしで先に固められる。
- 新規語彙は `observable_fidelity` の 1 つだけ。参照軌道を固定入力として渡す層で、**MD を走らせない**ので CI に載る。
- 力場一致は要件にしない。組成照合・参照軌道の再解析・ペア差分のいずれも参照の力場に依存しないため。
  「GPCRmd が CHARMM だから使いにくい」が効くのは絶対値を参照に合わせにいく設計だけで、それは採らない。
- 自前 MD と参照を絶対値で比べる層 (L4b) は σ_FF が測れる場合のみ作る。
  測定源は「GPCRmd 内で同一 PDB が CHARMM コアと Amber 投稿の両方に現れるペア」。無ければ作らない。

**次の 3 手 (いずれも MD 不要).** (1) MDDB の 1328 project から観測量ごとの σ_rep を算出、
(2) GPCRmd 773 ID をクロールして同一 PDB の力場違いペアを探索し L4b の可否を決める、
(3) `observable_fidelity` タスクを 10 本作る。

---

## 2026-08-18 — TMbed を導入し、膜配向を「探索」から「予測に従う」に変えた

別メンバーの SMO 膜構築が壊れた件を調べた結果、MEMEMBED / PPM が構造だけから
上下の向きを推定しており、大きな可溶性ドメインがそれを狂わせることが分かった。
TMbed (Bernhofer & Rost 2022, ProtT5 埋め込み + CNN + Viterbi) は配列だけから
TM セグメントと inside/outside を返し、Viterbi の文法が「TM を横切るたびに内外が
反転する」ことを強制するのでトポロジーが構造的に整合する。膜外の質量がいくらあっても
影響を受けない — まさに構造ベース手法が間違える所を埋める。

**OPM/PPM の 5L7D 正解に対する実測** (これが設計の根拠):

| | 膜法線の誤差 | 中心面ずれ |
|---|---|---|
| TMセグメントのヘリックス軸を平均 (完璧な境界) | **6.2°** | +0.4 A |
| 境界を ±5 残基ゆらす (TMbed の実精度) | 6.5 ± 1.0° (最悪 9.5°) | +0.5 ± 1.3 A |
| TM を1本まるごと見落とし | 最悪 11.3° | <= 0.7 A |
| 全TM残基をまとめて PCA | **22.0°** ← これはダメ |

要点は「ヘリックスごとに軸を出して平均する」こと。個々の傾斜は 10-37° とばらつき、
束全体の形は法線ではない。境界誤差には極めて頑健で、乱数を使う GA と違い決定論的。

**実データでの end-to-end**: TMbed を実際に 5L7D に走らせると n_terminal_side=out
(SMO の CRD は細胞外、正解) と 7 本の TM ヘリックスを返し、OPM 由来の正解境界と
±3 残基以内で一致した。その予測だけで配向すると OPM 正解を **z の RMS 1.26 A、
相関 +0.9995** で再現。ランダム剛体変換を掛けても結果は完全に同一 (回転不変)。
3セグメント分割構造 (A/B/C 鎖) でも鎖ごとに正しく処理した。

**実装** (4点):

1. `embed_in_membrane` に `--n-terminal-side in|out` を追加し MEMEMBED `-n` へ渡す。
   従来 MDClaw は `-n` を渡しておらず、**上下の向きを指定する手段が無かった**。
   あわせて `-s` を渡すようにし既定を 3 (GA 5回) に。MEMEMBED 自身の既定は 0 (GA 1回)
   で、packmol-memgen より探索が浅い状態だった。
2. 新サーバ `mdclaw/membrane_topology/` に `predict_membrane_topology`。構造 / 配列 /
   FASTA を受け、`membrane_topology.json` (segments, 側つき regions, n_terminal_side)
   を書く。構造入力なら author 残基番号をそのまま持ち回るので下流がそのまま使える。
3. `mdclaw/solvation/tm_orient.py` に決定論的配向。`--orientation-method`
   (auto/memembed/tm-segments) で選択、auto はトポロジーがあれば tm-segments。
4. geometry check にトポロジー整合性を追加。**上限としての overlap_fraction は使えない**
   ことが分かった: OPM 正解でも TMD のみ構築は 0.798、全長は 0.582 で、構築の性質に
   依存するため固定閾値に意味が無い。代わりに「非TM領域が予測された側にあるか」を見る。
   OPM 正解で 8/8、上下反転させると 0/8 と綺麗に分離する。閾値 0.75。

**モデルは SIF に焼き込む** (ユーザ指定)。TMbed の CNN 重みはパッケージ同梱だが
ProtT5 (2.3 GB) は初回実行時に HuggingFace から落ちる設計で、読み取り専用 SIF と
外部ネットワークの無い計算ノードでは失敗する。Dockerfile で `tmbed download
--model-dir $MDCLAW_TMBED_MODEL_DIR` を実行し、無ければビルドを失敗させる。
SIF は 4.6 GB → 約 6.9 GB になる見込み。

実系での統合確認: d473y の prep_004 から solv_004 を作り
`--membrane-topology-file` 付きで構築 → `orientation_method=tm-segments` が自動選択、
`n_terminal_side=out` を継承、geometry check が `topology_consistency 10/10 = 1.00`。
overlap_fraction 0.792 は MEMEMBED 構築の 0.798 とほぼ一致し、両者の配向が
一致していることも確認できた。

回帰テスト 17 本を `tests/test_membrane_topology.py` に追加。CLI contract golden を再生成。

**追記 (同日、レビュー中に判明した2件)**

(a) **beta barrel には効かない。** 「セグメント種別を区別していないが大丈夫か」と自問して
測ったところ、OPM 正解に対し 1QJP OmpA (8ストランド) は 2.0 度だが **2OMF OmpF
(16ストランド) は 14.5 度**。短いセグメントを混ぜると 41.7 度に悪化する (ここから
`MIN_SEGMENT_CA_ATOMS = 8` が実際に効いていることも確認できた)。バレルのストランドは
法線から ~40 度傾いて樽の周りを回るので、軸平均は弱い推定量になる。端残基の重心差という
別案も試したが改善しない (OmpA 10.0 度 / OmpF 15.5 度)。MEMEMBED には専用の `-b` モードが
あるので、そちらに回すことにした。

「そもそも barrel をどう認識するのか」は **TMbed が答える**。TMbed の出力クラスは H (ヘリックス)
と B (ストランド) が別で、論文 Table 1 で β-TMP recall 93.8 ± 7.5% / FPR 0.1 ± 0.1%、
Table 3 で TMB セグメント recall 95.0% / precision 99.2% と報告されている。

論文が「取り逃したバレルは全て 2-4 ストランドのもの」と書いていたので、そこが穴だと
推測したが **実測で否定された**: 7AHL α-hemolysin (protomer 2ストランド) → strand 2本、
1UUN MspA (論文が名指しした例) → strand 3本、4K3B BamA → strand 16本。いずれも
is_transmembrane=True で正しく分類。実バレル 5 件すべてが
`tm_orientation_beta_barrel_unsupported` で拒否され、SMO (helix 7本) のみ通ることを確認。
推測で限界を書かず測ったのが正解だった。

(b) **ビルドが TMbed ステップで失敗した (設計どおりのガード動作)。** 原因は `protobuf` 不足。
transformers が ProtT5 の SentencePiece トークナイザを展開するのに必要だが、TMbed が
依存として宣言していない。`SentencePieceExtractor requires the protobuf library` で落ちる。
`environment.yml` に追加。ローカル検証時は明示的に入れていたので露見していなかった。

**未対応**: SIF の再ビルドは進行中。それまで `predict_membrane_topology` は
`tmbed_unavailable` を返す (構造化エラーとして扱われる)。

---

## 2026-08-18 — 同一実行の失敗証跡が `observations/` に流れ、`trace_failure` が原因を言えなくなっていた

HPacker の cap バグを追う過程で気づいた別件。`trace_failure` が
`failure_code: null` を返し、`tool` / `argv` / `exit_code` もすべて null だった。
`skills/common/tool-output.md` は「安定した `code` で分岐せよ、`errors` を parse するな」と
定めているので、規約が想定する分岐材料が存在しない状態だった。実際これで誤誘導された。

**証跡は完全に採取されていた。置き場所だけが間違っていた。** 失敗は二段階で記録される:

1. ツールが `fail_node(...)` を呼び、貧弱な記録を `artifacts/failure/latest/` に書いて封印。
   (`fail_node` の呼び出しは repo 全体で 108 件あり、`code=` を渡しているものは 0 件。)
2. 直後に CLI の `_record_cli_node_failure` が `tool` / `argv` / `exit_code` /
   stdout・stderr tail を揃えた完全な記録で `record_node_failure` を呼ぶ。
3. ところが `node/failure.py` は「ノードが既に terminal なら後追い観測」と判定するため、
   **同一実行の記録が** `artifacts/failure/observations/<時刻>/` へ降格される。
4. `trace_failure` は `node.artifacts.failure` が指す `latest/` しか読まない。

実測 (`jobs/d473y/nodes/solv_001`): `latest/` は 04:35:25.660 に 599 B / 4 キー / `code: null`、
`observations/` は **32 ms 後**の 04:35:25.692 に 2870 B / 12 キー / code 完備。同一実行である。

修正は `record_node_failure` に `same_invocation: bool = False` を足し、CLI 側から
`same_invocation=True` を渡すだけ (差分 ~20 行)。true かつ既に `failed` なら observation 扱いを
やめて `latest/` を差し替える。**封印済み `node.json` は書き換えない** —
`tests/test_node.py` の不変条件であり、`trace_failure` は既に
`metadata.failure_code` → `manifest.code` → `tool_result.code` とフォールバックするので不要。
イベントも `terminal_node_failure_observed` と `node_failure_evidence_enriched` で区別する。

実系で確認: 壊れた `prep_002` から `solv_003` を作って同じ失敗を再現したところ、
`observations/` は生成されず `latest/` に code=`membrane_neutralization_failed`、
tool=`embed_in_membrane`、argv、exit_code=1、stdout/stderr tail が揃った。
`trace_failure` の `failure_code` も埋まり、`tool_result` は 4 キーから 17 キーになり
`hints` / `next_action` / `recoverable` も届くようになった。

**やらなかったこと。** 当初は `fail_node` 108 箇所すべてに `code=` を足す移行 (うち 96 件は
機械的置換) を計画したが、オーバーエンジニアリングと判断して撤回した。スキルが使う唯一の経路は
CLI であり、それは上記で完全に直る。副作用として `node.json` の `metadata.failure_code` は
空のままなので `inspect_job` の `failed_nodes` には `failure_code` が出ない。これは今回報告された
症状ではないので、必要になった時点で別途扱う。`membrane_neutralization_failed` の hint 文言
(「bulk water を増やせ」) が原因と無関係な件も、実益が薄いと判断して保留のまま。

回帰テスト 1 本を `tests/test_node.py` に追加。修正前のコードで失敗することを `git stash` で確認済み。
`ruff` clean、383 passed。

---

## 2026-08-18 — 訂正: cap 破壊の犯人は `prepare_complex` ではなく `create_mutated_structure`

**同日の前エントリ「SMO 5L7D D473Y / G497W membrane MD; `--cap-termini` produces
unparameterizable structures」の原因帰属を全面的に訂正する。** バグは実在するが、
`prepare_complex --cap-termini` ではなく **HPacker 経路 (`create_mutated_structure`)**
にあった。前エントリは変異後の `mutated.pdb` だけを見て prep の出力と取り違えていた。

中間ファイルを追った実測:

| ファイル | cap の位置 | 変異残基の H2/H3 | OpenMM |
|---|---|---|---|
| `prep_001/artifacts/merge/merged.pdb` (prepare_complex) | 正順 (idx 0,158,159,238,239,348) | なし | **OK 5470 particles** |
| `prep_002/artifacts/mutated.pdb` (create_mutated_structure) | 全て末尾 | SER190 に H2/H3 | **FAIL** |

`prepare_complex --cap-termini` は正常。`clean_protein.py:474-501` が PDBFixer の
`missingResidues` 経由で cap を入れており、配列位置も水素も正しい。

真の原因は `mdclaw/sidechain_packer.py` の 3 点で、すべて「ACE/NME を遊離ヘテロ原子と
みなす」ことに由来する:

1. `_write_protein_input` が標準アミノ酸の ATOM 行だけを HPacker に渡す (これ自体は正しい)。
2. `_rebuild_protein_hydrogens` がその **cap を欠いた** 構造に PDBFixer
   `addMissingHydrogens` をかける。PDBFixer は鎖トポロジから protonation を決めるので、
   cap の裏に隠れた残基が遊離荷電末端と判定され H2/H3 が付く。
3. `_split_hpacker_and_nonprotein_lines` が元の HETATM 行 (= cap) を全タンパク原子の
   **後ろに再追加** する。OpenMM は鎖内の残基「並び順」で結合を張るため cap が繋がらない。

修正 (`mdclaw/sidechain_packer.py`):

- `_is_terminal_cap_atom` / `_is_protein_or_cap_atom` / `_line_residue_key` /
  `_protein_and_cap_residues_from_lines` を追加し、cap を「タンパク側」として扱えるようにした。
- `_merge_caps_into_protein` を新設。HPacker 出力に cap を **元ファイルの並び順で**
  差し戻してから水素再構築に渡す。これで (2) と (3) が同時に消える。
- `_sort_protein_atoms_like_reference` と `_split_hpacker_and_nonprotein_lines` を cap 対応に。
- 副次的に見つかった潜在バグも修正: CONECT 行が入力の通し番号のままコピーされ、
  タンパク原子は水素再構築後の別番号で再出力されるため、`_normalize_pdb_lines` の
  serial マップが衝突して **CONECT が無関係な原子を指していた**。`_remap_conect_lines` を
  新設し、(chain, resseq, icode, atom name) の同一性で写像、解決できない行は捨てる。
  これを直すまで LEU346 が NME の水素と結合し `1 H atom too many` で落ちていた。
- guardrail code `hpacker_terminal_cap_merge_failed` を追加・golden 再生成。

検証: 実際の `merged.pdb` に `run_hpacker_mutation(mutations=['D473Y'])` を適用して
success、cap は idx 0,158,159,238,239,348 の正位置、SER190 の水素は H/HA/HB2/HB3/HG のみ、
CONECT 48 本を復元、**OpenMM 5479 particles で成功**。手作業で直した参照と一致。

回帰テスト 4 本を `tests/test_sidechain_packer.py` に追加 (cap の並び順、capped 末端の
非プロトン化、CONECT の同一性写像、解決不能 CONECT の破棄)。**4 本とも修正前のコードで
失敗することを `git stash` で確認済み**。`ruff` clean、主要スイート 222 passed。

なお `membrane_neutralization_failed` の hint (「bulk water を増やして再構築せよ」) が
実際の原因と無関係な点は前エントリの指摘どおりで、これは未修正のまま残っている。

---

## 2026-08-18 — SMO 5L7D D473Y / G497W membrane MD; `--cap-termini` produces unparameterizable structures

Ran the two vismodegib-resistance mutants of human Smoothened (5L7D, X-ray 3.2 A,
Byrne et al.) in a POPC bilayer, as a pipeline test at 100 ps production.
Study: `studies/smo_5l7d_vismodegib_resistance`, jobs `d473y` and `g497w`.

**Construct decisions.** Chain A, not B — chain B has a 492-506 gap that deletes
G497 outright. TMD only (residues 190-553): the BRIL fusion (numbered 1011-1131)
and the extracellular CRD are dropped, and with the CRD goes the only
crystallographic cholesterol, which binds the CRD (contacts 108-164) and sits
>40 A from the TM6/TM7 pocket — it is not a pocket ligand. Two unresolved gaps
(347-350, and 429-445 = the ICL3 that BRIL replaced) were kept as chain breaks by
splitting into three segments A 190-346 / B 351-428 / C 446-553 rather than
building a de-novo 17-residue ICL3. All four in-range disulfides retained,
including the inter-segment C314-C390.

**The run exposed a real bug: `prepare_complex --cap-termini` is broken.** Two
independent defects, both present at once. (1) ACE/NME are written as `HETATM`
records appended after every `ATOM` record, so a cap lands out of sequence
position in its chain — `ACE A 189` and `NME A 347` both end up after `LEU A 346`.
OpenMM links residues by order within a chain, so the cap never bonds. (2) The
residue following an ACE keeps its charged-N-terminus `H2`/`H3` on top of the new
bond to the cap. Fixing only (1) moves the error from *"missing 1 C atom"* to
*"matches NSER, but has 1 N atom too many"*; fixing both makes the same structure
build cleanly (5479 particles, verified).

This is invisible on the standard explicit-water path because `build_amber_system`
runs tleap, which sorts by residue number and rebuilds hydrogens. It surfaces in
`embed_in_membrane`, whose net-charge evaluation calls
`SystemGenerator.create_system` directly on the assembled PDB. The reported code
is `membrane_neutralization_failed` with the hint *"rebuild with enough bulk
water"* — **misleading**: bulk water was never the problem, upstream capping was.
Worth making that guardrail name the real cause.

Worked around by re-running prep with `--no-cap-termini`; the six termini are all
solvent-exposed at the membrane surfaces and ~25 A from the pocket, so charged
termini are an acceptable artifact at test scale. Should be fixed properly before
any production-quality run. Failed nodes (`prep_001/002`, `solv_001`) kept in the
DAG.

**Numbers.** 115,168 (D473Y) / 115,195 (G497W) atoms; 358/359 POPC; ~15.3k OPC
waters; 116 x 116 x 77 A box. Minimization 8.6e8 -> **-1,420,019 kJ/mol** and
5.8e13 -> **-1,425,417 kJ/mol**, max force 3.7e9 -> ~3.8e3 kJ/mol/nm — i.e. the
P18 membrane-min stall did *not* recur; the `patch-tile` backend tiles a
pre-equilibrated patch instead of packing the whole box, and that is what avoids
the lipid-tail clash trap. Equilibration 0.2 ns NVT + 1.0 ns NPT reached
301.0 +/- 0.9 K and 1.030-1.033 g/mL, compressing the under-dense tiled box from
0.906 g/mL (1083 -> 953 nm^3). Production 100 ps, 4 fs + HMR,
`MonteCarloMembraneBarostat` (XYIsotropic, ZFree, gamma=0): 300.9 +/- 0.8 K,
1.0376 +/- 0.0012 g/mL, CA-RMSD 0.71 A (D473Y) and 0.66 A (G497W) vs frame 0,
gross APL 66.7 A^2 before subtracting protein cross-section.

Verified the two things that fail silently: all 4 S-S bonds are present in
`system.xml` with original numbering (193-213, 217-295, 314-390, 490-507), and
the mutations survive tleap renumbering — topology index 262 is TYR in `d473y`
and ASP in `g497w`, index 286 is GLY and TRP respectively.

100 ps is a pipeline test, not sampling. No WT control was requested, so nothing
here supports a claim about either mutation's effect.

---

## 2026-08-18 — `eq` could silently skip `min`; found by running the onboarding guide

Wrote a RIKYU onboarding guide for the hackathon and ran it end to end as a new
member would — fresh clone under `/data1/rkp00048/$USER`, shared arm64 SIF, 4AKE
chain A, apo, 100 ps NVT + 100 ps NPT + 100 ps production on SLURM. It works:
`min` 18 s, `eq` 45 s, `prod` 38 s, **1 min 41 s of GPU time**, 300.34 +/- 0.90 K
and 1.018 +/- 0.001 g/mL. 4AKE is the open form, so at a 15 A buffer it solvates
to ~90,000 atoms — nearly twice 1AKE's 49,671.

**The run exposed a real bug.** Submitting `min` -> `eq` -> `prod` as
dependency-chained SLURM jobs means creating all three nodes before any of them
runs. `_auto_resolve_parent` walks `_AUTO_PARENT_PREFERENCE["eq"] = ("min",
"topo")` and falls through to the next entry whenever the preferred one has no
*completed* node. With `min_001` still pending, `eq` silently attached to
`topo_001` — equilibrating from the topology-time state and skipping
minimization entirely. `explain_node` reported `ready_to_run: true` with no
warnings, because `topo` is a legitimate `eq` parent for legacy DAGs.

The fix distinguishes *absent* from *not yet complete*: a less-preferred parent
type is now only reached when the preferred type has no nodes in the job at all.
Present-but-incomplete (or failed) returns `None`, so `create_node` demands an
explicit `--parent-node-ids` — the same structured `node_context_required` that
`prod` already gave in this situation. `_auto_parent_candidates` stops at the
same place so the error never advertises a `topo` candidate while a `min`
exists. Legacy `topo -> eq` DAGs with no `min` node are untouched.

This also covers `topo`, whose preference is `("solv", "prep")`: a pending
`solv` no longer falls through to `prep` and builds an unsolvated topology.

Verified in the live workflow, not just unit tests — re-running the guide, the
bare `create_node --node-type eq` now fails with `node_context_required` instead
of quietly mis-parenting, and the corrected chain gives a DAG with 7 completed
nodes, 0 failed, 0 orphaned.

---

## 2026-08-18 — Merged arm64 image verified on Rikyu; the shim contract holds

Pulled the merge onto Rikyu (`c000`, GB200, driver 580.173.02) and checked the
parts the merging host could not. The entry below flagged the
`MDCLAW_FUSEFIX_LIB` indirection as unverified because that host had no arm64
builder; it verifies clean.

**The build-time assertions pass.** The published SIF predates
`MDCLAW_FUSEFIX_LIB`, so it was injected to reproduce what a rebuilt image will
see. Both `RUN` assertions in `Dockerfile.rikyu-arm64` — the devel-stage
`LD_PRELOAD` check and the final-stage one that reads the variable — succeed.
The sanitized `container/mdclaw_fusefix.c` also compiles under the stricter
flags the Dockerfile now uses (`-Wall -Wextra -Werror -Wl,-z,relro,-z,now`,
gcc 13.3 aarch64), and the freshly compiled shim still fixes `torch.fft` on the
GPU. The rewrite that dropped the site-specific comments changed no behavior.

**The `check_declared` gate is presence-based in all three states**, tested on
the same SIF: undeclared → `SKIP` (20 passed), declared and correct → `PASS`
(21 passed), declared with a bad path → `FAIL` (20 passed / 1 failed). It cannot
silently pass.

`test-rikyu-gpu.sh` from the SIF: `ARM64_CUDA13_GPU_SMOKE=PASS`, including
`openmm_pme_cufft=PASS` and `pytorch_cufft=PASS` — the two checks the old script
lacked, which is why two broken images passed it 5/5 before the merge.
`test-container.sh`, 304 unit tests, and `ruff check mdclaw/ tests/` are clean.

**Fixed here: the SLURM GPU directive.** `_generate_sbatch_script` emitted
`#SBATCH --gpus-per-node=N`, which Rikyu's job-submit plugin rejects outright
(`[AI4S] Specify GPUs with --gpus=N (-G N). Per-node forms ... are not
supported`), so every `submit_job` with a GPU failed at submission. This is site
policy, not a bug — both spellings are valid Slurm — but the per-node form is
unusable on Rikyu, so both script generators now emit `--gpus=N`. The two forms
are equivalent at the default `--nodes=1` and differ beyond it: `--gpus-per-node`
is per node, `--gpus` is the job total, so `--nodes 4 --gpus 2` meant 8 GPUs
before and 2 now. Nothing in-tree submits multi-node GPU jobs (`nodes` defaults
to 1, and `skills/hpc-run` has no multi-node example), so `--gpus` now means the
job total everywhere. Keeping both spellings was rejected because it would leave
Rikyu with no multi-node GPU path at all. Validated end to end on Rikyu with
the fix in place: 1AKE chain A, apo, ff19SB/OPC, 49,671 atoms, submitted as
`min` -> `eq` -> `prod` with `afterok` dependencies (`--gpus=1`, 1x GB200).
All three `COMPLETED`; 100 ps NPT production held 300.6 +/- 1.2 K and
1.028 +/- 0.002 g/mL.

**Housekeeping:** `.gitignore` no longer excludes `RIKYU.md`, which says on its
first line not to commit it. `RIKYU-SIF-REBUILD.md` is superseded by
`docs/developer/container.md` plus the entry below.

---

## 2026-08-18 — arm64 MODELLER verified under emulation; glib was the missing piece

Registered `qemu-aarch64` binfmt on floyd (`docker run --privileged
tonistiigi/binfmt --install arm64`; host-wide, reversible with `--uninstall`)
and built a probe image that applies **only** the MODELLER block of
`Dockerfile.rikyu-arm64`, copied verbatim out of that file by the generator so
the probe cannot drift from the real build. Full emulated build of the rikyu
image was not attempted — measured qemu overhead is ~8.7x and the MODELLER step
was the only unverified part.

The tarball route works on real aarch64: `uname -m` = `aarch64`, the installed
`libmodeller.so.14` is ELF machine 183 (AArch64), `config.py` keeps the `XXXX`
placeholder, and with `KEY_MODELLER10v8` injected at run time `import modeller`
succeeds.

**Found by doing this, and only findable on arm64: `libglib-2.0.so.0` is
missing from the tarball.** `armv8-gnu/` bundles gfortran and hdf5 but not glib,
while the *conda* package does bundle it — which is why the x86 dry run passed.
`ldd` on `_modeller.so` shows glib as the one unresolved library. The real image
is fine because the conda environment at `/opt/mdclaw/lib` provides
`libglib-2.0.so.0` (confirmed in the new amd64 SIF) and the rikyu
`LD_LIBRARY_PATH` already puts that directory first — but that is an implicit
transitive dependency holding up a hard requirement, so two guards were added:

- `Dockerfile.rikyu-arm64` now runs `ldd` on `_modeller.so` after the install
  and fails the build on any `not found`.
- The shared smoke check no longer stops at parsing `config.py`; it does
  `import _modeller`, which loads the compiled object. That needs no licence —
  the licence check lives in `modeller/__init__.py`, after the extension
  imports — so it works on an unlicensed image. Verified in all three states:
  broken probe -> `AssertionError: MODELLER extension will not load:
  libglib-2.0.so.0 ...`, fixed probe -> `extension loads`, amd64 SIF -> 20
  passed / 0 failed, old SIF -> `SKIP`.

Still unverified: the full rikyu build end to end, and `test-rikyu-gpu.sh`,
which needs a real GPU and must run from the SIF.

---

## 2026-08-18 — Correction: MODELLER *does* run on arm64; rikyu gets it too

**This overturns the arm64 conclusion in the entry below.** I claimed MODELLER
could not run on arm64 Linux, that building on rikyu would not help, and that
only x86_64 emulation remained. That was wrong. I inspected the *conda package*
— which ships only `lib/x86_64-intel8/`, Intel-Fortran-linked — and generalised
from it to the whole distribution without checking the generic tarball.

`https://salilab.org/modeller/10.8/modeller-10.8.tar.gz` (38 MB) ships five
architectures: `armv6l-gnu`, **`armv8-gnu`**, `i386-absoft`, `i386-intel8`,
`x86_64-intel8`. `libmodeller.so.14` under `armv8-gnu` is `ELF 64-bit LSB shared
object, ARM aarch64`, gfortran-linked (`libgfortran.so.5`, no Intel runtime),
and the `Install` script detects `aarch64:Linux:*` and offers "5) Linux on
64-bit ARM". The conda channel is the limitation, not MODELLER.

The Python side works too: the tarball's `python3.3/_modeller.so` is a
stable-ABI (abi3) build, so one binary covers Python 3.3+. Verified on x86 by
importing the tarball's `python3.3` extension under the SIF's **Python 3.12** —
`import modeller` and `Environ()` both succeed. Same layout exists under
`armv8-gnu`, so rikyu's Python 3.12 is covered.

`Dockerfile.rikyu-arm64` now installs it from the tarball, laid out by hand
(modlib + src + bin/*.top + bin/lib + lib/armv8-gnu, symlinked into
site-packages) rather than via the interactive `Install`, matching the shape the
conda package produces so nothing downstream can tell the images apart. Proven
on x86 first with the equivalent `x86_64-intel8` layout — 57 MB, config.py left
at the `XXXX` placeholder, runtime `KEY_MODELLER*` injection working. Both
images now declare `MDCLAW_MODELLER_VERSION`, so both run the smoke check.

**Not verified:** the arm64 image was not built — this host's buildx offers only
`linux/amd64 (+4), linux/386` and `qemu-aarch64` binfmt is unregistered (needs
root). The tarball path needs a build on rikyu itself to confirm.

Emulation was measured before the tarball came up, and is no longer needed. For
the record, qemu-user does work: same 9UWI comparative model, native **13.5 s**
vs **116.6 s** under `qemu-x86_64-static` — **8.7x**, import 0.22 -> 1.22 s.
Same-arch TCG, so an arm64 host would differ somewhat, but the order stands.

---

## 2026-08-18 — MODELLER now ships in the amd64 image; two defects fixed on the way

`modeller_from_alignment` and the `modeller-predict` skill had **no working
runtime anywhere**. MODELLER was in neither `environment.yml`,
`container/Dockerfile`, `Dockerfile.rikyu-arm64`, nor `pyproject.toml`, so no
image could contain it; the SIF is read-only, so the skill's
`conda install salilab::modeller` advice was unreachable there; and
`check_model_backend --model modeller` answers `Available models:
['bioemu', 'boltz']`. Confirmed absent in all four local SIFs (0.6.5).

**The license was never the blocker.** mdclaw's runner builds a synthetic
`modeller.config` from `KEY_MODELLER*` and seeds it into `sys.modules` before
importing MODELLER, taking only `install_dir` from the installed config.
Verified against 10.8: installing with no key succeeds and leaves
`license = r'XXXX'`; an injected key is what MODELLER validates (a wrong one
fails with `check_lice_E> Invalid license key: FAKEKEY123`, naming the injected
value, not the placeholder). So the image ships the package unlicensed and each
user supplies a key at runtime.

Installed from `container/Dockerfile`, not `environment.yml`, because the
salilab channel is **linux-64 only** and the rikyu arm64 image derives its
environment from that same shared file. New image: 20 smoke checks pass
(`PASS: MODELLER installed`), SIF 5.2 GB at `mdclaw-modeller.sif`.

**arm64 is not portable, and building on rikyu does not change that.** salilab
publishes linux-64 and osx-arm64 but no linux-aarch64 and no noarch; bioconda
and conda-forge have nothing. `conda install` downloads prebuilt binaries, so
the build host is irrelevant. Nor can it be compiled: Salilab's own
`INSTALLATION` says *"The source code is not generally available"*; the shipped
`src/` holds only 45 SWIG `.i` files and headers, there is no build system, and
the one Linux target `lib/x86_64-intel8/` links the Intel Fortran runtime
(`libifcore.so.5`, `libimf.so`), which has no ARM build. Only Salilab can fix
this.

### Two defects found by actually using it on 9UWI

1. **Models came back in MODELLER's own frame, numbered from 1.** Fine for de
   novo homology modeling, wrong for the `loop_refinement` repair case the skill
   advertises. On 9UWI chain A (V1aR; 269 resolved, 40 missing over three gaps
   incl. a 33-residue ICL3) the returned model sat **9.86 A** CA RMSD from its
   own template, numbered 1..309 instead of 43..351 — so the atosiban taken from
   the same cryo-EM entry landed in the wrong place, with nothing in the output
   saying so. New `--template-frame` refits and renumbers via the PIR alignment:
   **9.858 -> 0.484 A** over 269 paired CAs, 309 residues renumbered to 43..351,
   and the receptor/atosiban interface returns at **311 of 324** crystal contacts
   with zero clash under 2.0 A. The in-place deviation is now always reported.

2. **The frame check read the wrong alignment file.** `AutoModel.auto_align()`
   aligns the seed, writes the result beside it as `<alnfile>.ali`, and leaves
   the seed untouched with an empty template entry. The first implementation read
   the seed, found no template residues, and skipped restoration on every
   auto-aligned run — the exact case it was written for. Its warning said the
   alignment "does not contain both 'v1arA' and '9uwiA'" while printing "found
   ['9uwiA', 'v1arA']", because one branch handled missing and empty entries.

Tests: `tests/test_modeller_template_frame.py`, 6 cases, no MODELLER needed.
162 passed across genesis/registry/cli.

**Not done:** 9UWI itself is parked at `source_001` (fetch complete,
`solvent_regime=membrane`). Atosiban's GAFF parameterization — `MPT`,
`A1EQM` (O-ethyl-D-Tyr), `ORN`, `NH2` plus an MPT-CYS thioether macrocycle — is
untried and is the likely next obstacle.

---

## 2026-08-18 — Rikyu arm64 image merged to main; one smoke test now serves both

`container/rikyu-arm64` (13 commits, last touched 2026-08-01) is on `main` as a
merge commit. Both Dockerfiles now live side by side and share
`environment.yml`, `pyproject.toml`, `container/scripts/test-container.sh`, and
`docs/developer/container.md`:

| | `container/Dockerfile` | `container/Dockerfile.rikyu-arm64` |
| --- | --- | --- |
| arch / CUDA | x86_64, 11.8 | arm64, 13.0 (NVRTC) + 13.1 math libs |
| OpenMM | 8.2.0 | 8.5.1, `openmm-torch` at `sm_100` |
| publishes to | `ghcr.io/matsunagalab/mdclaw:latest` | `ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-<rev>` |

**The merge itself was nearly clean.** One conflict: the MDAnalysis floor, main
at `>=2.7` from v0.6.5 and the branch at `>=2.8,<3` because linux-aarch64
conda-forge builds start at 2.8. Took `>=2.8,<3` — satisfies both, matches
`environment.yml`, and the published image already carries 2.10.0.

**Sharing the smoke test was the part that actually broke.** Two of the checks
the branch added assume the arm64 image: the cuFFT contract globs
`libcufft.so.12.*`, and the shim contract requires `libmdclaw_fusefix.so` in
`LD_PRELOAD`. Run against the published amd64 SIF, the merged script gave
**19 passed / 2 failed**. Fixed with `check_declared <VAR> <desc> <cmd>`, which
skips when the image never declared the contract. Same script, same SIF:
**19 passed / 0 failed**, two `SKIP` lines. The gate is presence-based rather
than a permanent no-op — forcing `MDCLAW_CUFFT_MIN_VERSION` and
`MDCLAW_FUSEFIX_LIB` into the amd64 SIF reproduces both failures, so the arm64
image (which sets both) is still held to them.

`MDCLAW_FUSEFIX_LIB` is new and is now the single definition of the shim path;
the Dockerfile's runtime assertion and `test-rikyu-gpu.sh` read it instead of
repeating the literal.

**Not verified here:** the arm64 image was not rebuilt — no arm64 builder on
this host. The `MDCLAW_FUSEFIX_LIB` indirection touches a build-time `RUN`
assertion in `Dockerfile.rikyu-arm64`, so the next Rikyu build is the first real
test of it. Nothing is pushed; `main` is local-only and ahead of `origin/main`.

---

## 2026-08-15 — Baseline 0.875, then three fixes before the real K=3

`passk3_20260814_v2_pi_rep1` finished 40/40: **overall_score 0.875**, five tasks
`failed` with `missing_raw_artifacts` — the four 60-min timeouts (P09, P13, P19,
P24) plus P36. 11.7 h of task time, of which **4.0 h (34 %) went to those four
hung commands**. This run is the pre-fix baseline; it cannot be one of the three
repeats, because the fixes below change the system under test.

**1. `parse_mutation_specs` accepts any separator, and says so when it doesn't.**
`--mutations` is `nargs="+"`, so the CLI wants `--mutations L99A M102Q`. P09's
agent passed `"L99A,M102Q"`, `_MUTATION_RE` rejected the single token, the node
was sealed terminal, and the agent spent its remaining 55 minutes grepping the
repo for why. Now each token is split on `[,\s]+` first, and the error names the
multi-mutation form instead of only the single-mutation notation. Four
separator forms are covered by tests; mutation-tested 3/3 (drop the split, split
on whitespace only, drop the hint from the message).

**2. Per-command watchdog for the agent** —
`MDPrepBench/tools/pi_shell_timeout.sh`. pi resolves its shell through
`settings.json` `shellPath` and invokes it as `<shellPath> -c "<command>"`
(`pi-coding-agent dist/utils/shell.js`), so pointing that at a wrapper caps every
command. 600 s: the longest legitimate command in the July 40-task run was
257.7 s (p99 63 s). The setting is global but the wrapper only wraps when `$PWD`
is inside a MDPrepBench run, so other pi sessions are untouched
(backup: `~/.pi/agent/settings.json.bak-20260815`).

Verified separately, not end to end. Proven: pi does route through the wrapper
(logged a real `-c echo …` invocation, one `-c` per command, so no persistent
session shell to kill); and the wrapper caps correctly (exit 124, process-group
kill confirmed with marker files, partial output still returned). Not proven: a
long command inside pi actually returning 124 — repeated attempts stalled in pi
before it issued any tool call, and **a control run with `shellPath` removed
stalled identically**, so that is pi flakiness, not the wrapper. Check the first
hour of the run for any tool call over ~615 s. Worst case equals rep1's
behaviour, so this cannot make things worse.

**3. `MDCLAW_RUNTIME=singularity` in the sweep env.** `bin/mdclaw` probes for a
conda env before falling through to Singularity, and `conda env list` costs
1.3 s on a host that has no `mdclaw` env. Measured `bin/mdclaw --version`:
**3.8 s → 2.4 s**. Left as an env var rather than a code change — the probe is
correct on hosts that do have the env.

---

## 2026-08-15 — Correction of the correction: there is no pi floor, and the four timeouts are hung commands

The entry below claims a "quantisation floor inside the pi harness" and cites
`cat common/run-loop.md` taking 7.19 s. The cursor advisor refuted it and I
verified both claims against the transcripts myself.

**1. The 7 s "floor" was a batching artifact.** When one assistant message
issues several toolCalls, all their results are recorded at one timestamp, so
every sibling inherits the slowest one's elapsed time. Measured over rep1:

| | n | median | in the 6.4–7.6 s band |
|---|---|---|---|
| plain shell, `batch=1`, no mdclaw | 466 | **0.11 s** | 2 |
| plain shell, batched, no mdclaw | — | — | 25, **all 25 with an mdclaw sibling** |

The `cat` at 7.19 s was batched with an mdclaw call. There is no harness floor.

**2. The transcript timing method is accurate.** The agent itself ran
`time mdclaw …` 17 times. Comparing the shell's own `real` against the
toolCall→toolResult window: **median gap 0.05 s**, across commands from 0.55 s
to 263 s. So toolCall→toolResult *is* the command's wall time — no harness
overhead to subtract. That kills the "~3 s recording overhead" theory too.

**3. The four 60-min timeouts are not model generation.** Each burned 42–55
minutes inside a single environment-probing command:

- P09 — `grep -rln "mutation_spec_invalid\|hpacker" <benchmark_runs tree>`, toolCall never answered
- P24 — a hand-written `min.py` on the host venv, toolCall never answered
- P13 — 42.2 min in one completed call: `which tleap parmchk2 antechamber; ls /opt/anaconda3/...`
- P19 — 51.7 min in one completed call: host-venv python probing `openmm.__file__`

My earlier reading ("P09 spent 3.1 min of 60 in tool calls, so the rest is
generation") was an artifact of my own script: it yielded only calls that had a
matching toolResult, so a hung command was invisible, and batch double-counting
inflated the others. Same failure mode as P26 in the entry two below — the agent
leaves the workflow and scans a huge tree.

**4. The lazy-preload win does not show up in the benchmark.** Solo, read-only
mdclaw calls: July min 5.71 s / median 6.07 s; now min 6.44 s / median 7.00 s.
The floor went **up ~0.9 s** since July. The direct A/B (5.91 → 2.90 s) is real
and reproducible, and the transcript timings are trustworthy per (2), so the two
must be measuring different work — benchmark calls are `create_node` /
`explain_node` / `inspect_job` against a job dir on NFS, not `--list-json`.
**Unresolved.** Do not claim the fix sped up the benchmark.

**5. Verified MDClaw bug behind P09.** `--mutations` is `nargs="+"`
(`mdclaw/_cli.py:450`), so the correct form is `--mutations L99A M102Q`. The
agent passed `"L99A,M102Q"`; `_MUTATION_RE` (`mdclaw/sidechain_packer.py:178`)
anchors a single token, so it cannot match, and the message
(`mdclaw/sidechain_packer.py:196-198`) is *"Invalid mutation spec 'L99A,M102Q'.
Use L99A or A:L99A notation."* — it never says how to pass more than one. The
task asks for two mutations. The agent tried `"L99A M102Q"` quoted, failed
again, and went grepping. The failure also sealed `prep_002` as terminal, so
retrying on that node was refused.

---

## 2026-08-14 (later) — Correction: the "per-mdclaw-invocation latency" numbers in the entry below are pi's floor, not mdclaw's cost

The entry below reports light-call latency of 6.04 s (July) vs 7.19 s (Aug) and
treats it as mdclaw's per-call cost. **It is not.** In the same transcripts:

```
cat common/run-loop.md                        7.19 s
ls -la && grep -c "^ATOM" 2LZM.cif ...        6.92 s
[read] .../solver_workspace/.agents/skills/…  7.43 s
```

`cat` of a local file does not take seven seconds. A histogram of all 1511 tool
round-trips in the running sweep is bimodal — 732 calls under 0.5 s, a near-empty
valley from 3–6 s, then 385 calls piled at 6.5–7.5 s. July shows the same shape
with the pile at 6.0–6.5 s. That is a quantisation floor inside the pi harness,
applied to anything that does not return almost instantly, and it is what those
medians were measuring. The floor rising ~0.7 s between July and August is a pi
change, not ours.

So the lazy-preload win is real but invisible here. Same-moment A/B in the
benchmark's own solver workspace, while the sweep was running:

| | median |
|---|---|
| checkout, lazy preload | **2.90 s** |
| SIF baked package, import-time preload | 5.91 s |
| via `bin/mdclaw` (adds the wrapper) | 3.65 s |

pi reports all three as ~7 s. Benchmark wall time cannot measure mdclaw CLI
latency; only direct timing can.

**Second finding, not yet fixed:** `bin/mdclaw` calls `_conda_env_exists()`
before falling through to Singularity, and `conda env list` costs **1.06 s** of
the 1.30 s wrapper overhead — on a host that has no `mdclaw` conda env at all.
Reading `~/.conda/environments.txt` answers the same question instantly. Holding
the change until the K=3 sweep finishes so the three repeats stay comparable.

**Third:** the 60-min cap is being consumed by model generation, not by tools.
Of the four timeouts in rep1 so far, P09 spent 3.1 min of its 60 inside tool
calls and P24 spent 0.3 min. Making the CLI faster cannot fix those.

---

## 2026-08-14 — The pass^k sweep ran slow: contention, not the refactor — but it exposed a 3.4 s tax on every CLI call

I stopped the K=3 pass^k sweep at rep1 19/40 because tasks were taking ~1.7×
the July wall time, and went looking for a regression in the de-over-engineering
work. **There is none.** What the transcripts actually show:

| metric (21 tasks in common) | July 20 | Aug 14 |
|---|---|---|
| per-`mdclaw`-invocation latency, light calls | 6.04 s median | 7.19 s median |
| `build_amber_system` (CPU, tleap), same tasks | 1026 s total | 938 s (0.91×) |
| `solvate_structure` (CPU), same tasks | 701 s total | 771 s (1.10×) |
| `run_minimization` (GPU), same tasks | 384 s total | 868 s (**2.26×**) |

Only the GPU step inflated, and only for the first ~4 h of the run: hourly
medians went 121.8 -> 99.0 -> 79.7 -> 48.7 -> 14.2 -> 10.5 s, i.e. back to
July's 13–19 s by +5 h. Same task, same system, `platform: CUDA` in both,
identical `max_iterations`, `restraint_count` (1309) and final energies. That is
host contention — this box shares 7× A6000 with other jobs — not code.

**The `bin/mdclaw` PKG_ROOT bind is not the cost either.** A/B, 10 reps each,
`mdclaw --list-json inspect_molecules`: with the bind + NFS `PYTHONPATH`
5.82 s, against the SIF's baked package 6.06 s. Within noise, and the bind side
is if anything faster. My earlier "+1.7 s per call" was a cold-cache artifact.

**What the hunt did find: `import mdclaw` cost 4.6 s, of which 4.5 s was
`import torch`.** `mdclaw/__init__.py` ran `_preload_torch_for_openmm_torch()`
at import time — every CLI call, including `--list-json`, `inspect_job` and
`create_node`, dlopened libtorch's CUDA libraries to keep the openmm-torch
plugin working (the June-29/July-7 fix for PythonTorchForce). Breakdown inside
the SIF: singularity `exec true` 0.38 s, + python start 0.46 s, + `import
mdclaw` 4.51 s, + full `_discover_tools()` 5.81 s.

Two measured facts turned that into a fix:

1. `importlib.util.find_spec("torch")` gives the library path without executing
   torch: 3.48 s -> 1.46 s.
2. The dlopen does **not** have to precede `import openmm`. It must precede the
   *plugin scan*, and the scan can be re-run: dlopen, then
   `Platform.loadPluginsFromDirectory(Platform.getDefaultPluginsDirectory())`,
   and the CUDA kernel registers. Verified three ways on an A6000 — `early`
   (today's order) OK, `late` without a rescan fails with "Platform does not
   support the requested kernel" exactly like no preload at all, `late` **with**
   the rescan OK (71.25 kJ/mol from a real PythonTorchForce Context).

So the preload moved out of `mdclaw/__init__.py` into
`custom_forces._preload_libtorch_cuda()`, called from `_import_openmmtorch()` —
the one code path that needs it. `MDCLAW_PRELOAD_TORCH_FOR_OPENMM` is gone; a
knob for a cost nobody pays any more is just more surface.

Result, 8 reps each: **5.91 s -> 2.62 s per CLI call (2.26×)**. At ~30 mdclaw
invocations per MDPrepBench task that is ~100 s/task, ~11 % of a 15-min task,
and ~3 h off a 120-task K=3 sweep.

`tests/test_torch_preload.py` was rewritten for the new contract and
mutation-tested: dropping the rescan, reversing the c10/torch_cuda order,
dropping RTLD_GLOBAL, rescanning on CPU-only torch, going back to `import
torch`, and re-adding the preload to `mdclaw/__init__.py` are all caught.

**Lesson.** Two of my three suspects (the PKG_ROOT bind, "the model got slower")
were wrong, and the July-vs-today medians said so within minutes — but only
after I stopped comparing *inter-event gaps* and started comparing *the same
step on the same task*. Aggregate latency hides which layer moved; a paired
comparison names it.

---

## 2026-08-14 — Why P26 kept timing out: it never entered the workflow

`P26_prep_zinc_metalloenzyme_2cba` (carbonic anhydrase II, catalytic Zn) was the
only MDPrepBench task pi + deepseek failed repeatedly — 3 timeouts in 4 attempts
against a 30-min cap, while P27 (Mn), P30 (Zn+DNA) and P06 (Ca) passed first try.

**One thing separates the runs.** Both successes ran the canonical workflow
(bootstrap -> inspect_job -> create_node -> explain_node -> inspect_molecules ->
prepare_complex -> minimize). All three failures reached none of it: two called
no workflow tool at all, one only introspected `--list-json prepare_complex`.
And every failure ends on a filesystem-wide `find /` — neither success runs one.
This host mounts ~390 TB of NFS under `/` (117T + 99T + 98T + 73T); measured,
`find / -name ions.xml -path '*amber*'` does not finish in 60 s. One such command
consumes the whole remaining budget, which is why the transcripts stop at 2.2 /
16.6 / 17.9 min but the runs die at 30.

So the chain is: skip `inspect_molecules` -> never learn the default water XML
already covers ZN -> go establish it yourself -> grep force-field XMLs ->
`find /` -> budget gone.

**Both of my hypotheses were wrong, and the evidence says so.**

- "The CLI cannot answer whether Zn is supported." False. On the real 2CBA,
  `inspect_molecules` already returned `metal_parameterization_required: false`
  plus a note that the default OPC water XML provides the templates. My first
  check used a bare-ZN-only stub PDB that never reached the metal-detection path
  — the test was wrong, not the tool.
- "My skills consolidation buried the ion policy by deleting
  `skills/md-prepare/ion-policy.md`." Refuted decisively by the advisor: the
  July P26 success never read that page (only the spine and explicit-water.md).
  Nor did July's P27, which instead parsed `amber19/opc.xml` inside the
  container by hand. The over-verification habit predates a9c6255 entirely.

**Fixed anyway, where the agent actually looked:**

- The verdict was prose in `notes.metal_handling`, far from the ion guidance.
  `preparation_guidance.ions` now carries stable values — `bare_ion_templates`,
  `bare_ion_templates_water_model`, `bare_ion_templates_scope`. The scope name is
  deliberately narrow: templates existing for a bare ion is not a claim that the
  coordination site is scientifically modelled.
- `metal_parameterization_required` was hardcoded `False` regardless of the
  catalog check. Latent today (every multivalent metal in the detector is in the
  OPC catalog) but a lie waiting to happen; now derived.
- `explicit-water.md` told the agent that finding multivalent metals means
  finishing "the matching explicit prep branch". A standard bare ion needs no
  branch, and that sentence invites exactly the investigation that killed these
  runs. It predates a9c6255.
- `--list-json <node tool>` said `job_dir` and `node_id` are required without
  saying where they come from — the agent that introspected before calling got a
  parameter list and no way in. Node-required tools now carry `workflow_entry`.

Skills net -3 lines (the duplicated ion sentence in `prepare-complex.md` and the
page-hunting route in `SKILL.md` are gone); no new tool.

**What this does not establish.** One passing run would not prove anything: the
pre-fix state also passed 1 in 4. Divergence is model-level variance and these
changes do not forbid it — they put the answer and the way back where a
diverging agent was already looking. The reliable guard is at the harness shell
boundary (refuse `find` rooted at `/`, `/home`, `/data*`; cap discovery commands
and kill the process group), which belongs to MDPrepBench, not MDClaw.

**Also learned:** pi's provider config changed. `spark1-vllm` is gone;
`deepseek-cloudflare/deepseek-v4-flash` now points at the same local vLLM
(`http://192.168.1.61:8000/v1`). The first rerun died in 0 min on
`Model "spark1-vllm/deepseek-v4-flash" not found` — an environment change, not a
code one. The memo entry of 2026-08-13 that called the spark1 name the real one
is superseded.

---

## 2026-08-13 — v0.6.5: MDAnalysis in, image rebuilt, and a lint that started screaming

The runtime image was rebuilt so this week's simplification actually ships, and
MDAnalysis was added beside mdtraj. Both went into one build rather than two:
`container/Dockerfile` copies `mdclaw/` before the conda stage, so any source
change forces a full rebuild including the OpenMM source build (~1 h), and
sequencing them would have cost that twice plus a second ~15 GB push for the
same end state. The dependency risk was retired first — `pip install --dry-run
MDAnalysis` inside the published image showed 2.10.0 resolving with no
numpy/scipy movement, adding only GridDataFormats, mmtf-python, mrcfile,
msgpack and threadpoolctl.

MDAnalysis is declared in `pyproject.toml` next to mdtraj, which is the one
place that reaches both targets: the conda env through `environment.yml`'s
`pip: -e .`, and the image through `pip install ".[dev]"` in stage 1.

Version bumped to 0.6.5 because `bin/mdclaw` derives the default Docker tag
from `plugin.json`: leaving it at 0.6.4 would either strand Docker users on the
old image or redefine a published release tag. `:0.6.5` and `:latest` now share
`sha256:6d5ff025…`; `:0.6.4` is untouched.

Verified on the image (19/19 container tests, GPU) and again on the SIF: 77
tools, v0.6.5, MDAnalysis 2.10.0, mdtraj 1.11.1, CUDA present, and — checked
deliberately — the *baked* package answers an unknown tool with
`tool_not_available` JSON and a renamed one with its replacement. That check
matters now that `bin/mdclaw` binds the checkout: ordinary work would no longer
notice a stale baked package, but plugin users run exactly that copy. Full
suite on the new SIF: 1349 passed, 3 skipped.

**What the swap exposed.** ruff went 0.15.21 → 0.16.2, and since
`pyproject.toml` selected no rules, `ruff check mdclaw/ tests/` — the command
CLAUDE.md tells contributors to run — went from clean to **1,492 findings**
overnight. Nothing in the code changed; the defaults widened. A lint that
always screams is a lint everyone learns to ignore, which is the same failure
mode as the flaky test in the previous entry. The rule set the code was written
under (`E4, E7, E9, F`) is now pinned, and both ruff versions agree on the
result.

Pinning then surfaced three unused imports I had introduced and not seen,
because my final lint runs had narrowed to `mdclaw/` and skipped `tests/`. It
also left the 17 pre-existing E702/E741 violations in two test files visible;
those are fixed too, so the documented command is actually green rather than
green-if-you-ignore-the-usual-noise.

**Unresolved host issue:** `/` is at 100% (5.2 G free), which is what made the
first `singularity pull` fail — SIF conversion was redirected to `/home` via
`SINGULARITY_TMPDIR`. Docker holds 221 GB of images and 190 GB of build cache,
371 GB reclaimable. Left alone deliberately: pruning the cache makes the next
image build much slower, and that is the maintainer's call.

---

## 2026-08-13 — Independent review of the simplification, and what it found in the tests

A codex advisor (gpt-5.6-sol, xhigh) was stood up in a Herdr pane and asked to
review commit a9c6255 without being told what to conclude. It found real
defects the author's own tests had not, and its second pass — an audit of the
test suite itself — found more. Both passes verified every claim by mutation:
break the implementation, check whether the test notices.

**Defects the review found in a9c6255** (all fixed):

- Seven of the twelve removed tools fell through to an argparse dump on stderr
  (exit 2, empty stdout), breaking the "every failure is JSON on stdout with a
  stable code" contract. The advisor framed this as an incomplete compatibility
  layer and recommended restoring aliases; the maintainer's question — "why
  care, we deleted them?" — produced the better diagnosis. Measurement showed a
  never-existed name behaves identically, so this was a pre-existing hole in the
  CLI that the deletions merely joined, and the fix is one generic
  unknown-subcommand handler, not a seven-entry tombstone table (which would
  have re-created exactly the hand-maintained name list this refactor deleted).
  `--list-json` already answered such names correctly, so both paths now share
  one resolver.
- Three migration hints silently dropped the old tool's defaults
  (`setup_model_backend` requires `--model`; `fetch_structure` defaults to CIF
  where `get_alphafold_structure` defaulted to PDB), and the comment above
  `_RENAMED_TOOLS` still claimed the Python functions survived for direct
  importers — false since a9c6255 deleted them.
- `search_structures` still advertised the deleted 0–120 MD-suitability rubric
  (`ranking_method: "md_suitability"`, `md_score_info` with interpretation
  bands) while computing a plain method-then-resolution sort, and a skill page
  claimed chain composition entered the ranking.
- **A regression this refactor introduced**: routing `setup_logger` through the
  root logger made merely importing mdclaw attach a root handler, so a host
  application's own records started printing. Fixed with a package-level
  NullHandler; `literature/_base.py` turned out to have been doing the same
  thing via `logging.basicConfig` since well before this work.
- budget validation, loosened to a shape check because "no Python code reads
  these numbers", had the wrong test: the reader is a later *agent*
  (`md-production` takes production length from `derived.target_*`). Restored
  as enums/types/signs only — and the first restoration was itself buggy
  (`headroom_hours` unchecked rather than sign-unconstrained, explicit nulls
  passing, enum checks raising TypeError on list input, NaN/Infinity accepted).

**What the test audit found.** The suite was green throughout, which turned out
to mean less than it looks:

- `test_direct_args_win_over_structure_analysis` asserted nothing at all. A
  first fix made it assert — and mutation testing showed it *still* passed with
  the precedence inverted, because the rule applies to what reaches
  `clean_protein`, and that block never runs when the fixture returns no
  proteins. The original author knew ("full precedence is exercised in the
  end-to-end test") but no such test exists. Now stubs a protein through and
  inspects `clean_protein`'s actual kwargs; mutation fails it.
- `test_removed_tools_are_deliberate` did not implement its own docstring: it
  claimed to fail when a server still imports, but used a hardcoded core-server
  list — which still named the deleted `benchmark` server, and let any
  non-core tool vanish silently.
- Tests pinned prose where the contract is a code: rewriting a failure's `code`
  to `unhandled_error` left them green. `test_representative_tool_failures`
  hid this structurally by passing raw results straight through
  `finalize_error`, which defaults a missing code to `unhandled_error`.
- `_run_cli` discarded the exit status; the guardrail-registry tests skipped
  (rather than failed) if the registry went missing; two live-API tests were
  unconditionally skipped placeholders behind a `--runslow` flag that does not
  exist; stage mappings survived for two tools deleted in a9c6255.

The lesson worth keeping: a green suite is evidence that the tests pass, not
that they guard anything. Every fix in this entry was checked by breaking the
implementation first. Also, a reviewer can identify a real defect and still
recommend the wrong remedy — the unknown-tool finding was correct, its proposed
fix would have partly undone the simplification.

**A flaky test that predates all of this.** The full suite then failed on
`test_embed_in_membrane_runs_parallel_packmol_race`. It is not a regression:
at HEAD it passed 2 of 5 runs, and at `dce72c6` — before any of today's work —
1 of 5. Every "full suite green" claim in this memo, including today's, was
partly luck. The implementation is right: the race cancels lanes that have not
started once a winner is accepted, and a sibling test exists for exactly that.
The test's premise was wrong — it assumed all four lanes always reach the
runner, so whenever one lane finished before the last was scheduled, the
cancelled lane went unrecorded and the count came up short. Fixed with a
`threading.Barrier(4, timeout=30)` in the stubbed runner: every lane must
arrive before any returns, which is deterministic (10/10) and still fails
loudly if a real regression starts fewer lanes. Worth noting that a test
failing 40–80% of the time was in a position to hide someone's real regression
for as long as it existed — the same failure mode the 2026-08-11 entry
describes.

Full suite after all of the above: 1349 passed, 3 skipped, 0 failed; ruff clean.

Still open: old job dirs carry `claim` metadata that the agent-facing index no
longer surfaces (a migration warning was proposed, not written), and
`test_registry` still skips on any ImportError, so an accidental import typo in
a server can hide its tools from discovery without failing anything.

---

## 2026-08-13 — MDPrepBench pi+deepseek revalidation after the simplification: 40/40 at 1.0

The de-over-engineered tree (previous entry) was revalidated with the same
solver as the 2026-07-20 sweep: pi (`pi-user` profile) +
`spark1-vllm/deepseek-v4-flash`, skills+cli, 30-min/task cap, deterministic
scoring — now through the standalone MDPrepBench repo (`~/tmp/MDPrepBench`,
runs `refactor_verify_*`). **Every one of the 40 tasks scored 1.0**, one task
better than July's 39×1.0 + P28 0.9639 (P28 scored 1.0 this time). The
consolidated skills and the 77-tool CLI carried the whole suite, including the
new `--lipids` list contract (P18/P34/P37/P39 membranes all 1.0).

Caveats worth the record:

- **Not one-shot.** The first pass ran as two concurrent shards to halve
  wall-clock; that self-inflicted vLLM congestion produced 6 walltime timeouts
  (P03, P21, P24, P26, P28, P29 — 5 of 6 in the same shard). Sequential
  retries passed 5 of them at 1.0 immediately. July's 40/40 was sequential;
  concurrency, not the refactor, was the variable — P37–P40 sped up the moment
  shard A finished.
- **P26 (zinc, 2CBA) needed 4 attempts.** Attempts 1–3 timed out the same way:
  with a byte-identical prompt and identical skills, the agent ignored the CLI
  and spelunked openmm data dirs for ion XMLs, ending in `find /` scans. The
  first assistant sentence already diverges from July's run ("inspect the local
  OpenMM environment" vs July's "read the relevant skills"), and the spark1
  serving config changed since July (220K → 1M context on the same model
  name) — model-side drift/variance, not a skills regression: P27 (Mn) and
  P30 (Zn) passed 1.0 first try, and attempt 4 passed 1.0 in 20 min via the
  normal CLI path.
- **The harness now really tests the checkout.** `bin/mdclaw` previously ran
  the SIF's baked-in mdclaw package while host-side native tools ran the
  checkout — a silent version skew. It now binds PKG_ROOT and sets PYTHONPATH
  into the container, so these runs exercised the refactored source, verified
  by tool count (77) from inside the solver workspace.

---

## 2026-08-12 — De-over-engineering executed: −6,433 net lines, 89 → 77 tools

The audit below was executed the same day: 148 files changed, +1,274 / −7,707
(net −6,433). Suite green afterwards (1,252 passed to the old stop point plus
the tail files; ruff clean on `mdclaw/`). Skills: 4,332 → ~3,290 lines,
61 → ~46 files. `_cli.py` 1,409 → ~1,113; `evidence/reporting.py` 1,683 → 503.

**Deleted outright:** claim/lease machinery + its guardrail codes; `update_node`;
`find_nodes`/`get_children`; `mdclaw/metal/`; `research/structure_analysis.py`
(its two disulfide helpers moved to `structure/disulfide.py` — the audit missed
that `prepare_complex` imports them); `research/scoring.py` (the 0–120 rubric;
`--rank-for-md` now sorts X-ray→cryo-EM→NMR, best resolution first); the
evidence Methods half + `citation_inventory.md` + `evidence_schema.py` (folded);
alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`) — all now `tool_renamed`
redirects; PLIP; write-only `artifact_sha256` (existence check kept — no more
hashing multi-GB trajectories inside node.lock); the `_tool_meta` shims; false
MCP docstrings (`test_mcp_server.py` → `test_registry.py`); stale
`mdclaw/benchmark/` and `tests/test_benchmark/` pycache ghosts.

**Refactored:** one `_tool_param_specs` pass now feeds argparse, `--list-json`,
and kwargs assembly (the triple type-dispatch ladder is gone);
`embed_in_membrane.lipids` is `list[str]` (the 60-line repeated-string CLI
special case died); `fetch_structure` defaults `source="auto"` (CLI convenience
layer died); benchmark JSONL hook moved to `_benchmark_log.py`; `setup_logger`
propagates to one root handler (stream-swap surgery collapsed); TOOLS/`__all__`
derived from function objects in all 16 package `__init__`s; glycan helpers and
`CANONICAL_WATER_MODELS` moved to `chemistry_constants`; study log
triple-wrapper inlined; budget validation reduced to shape-only (field-level
tests replaced accordingly); prod-chain walkers unified; node.json readers
collapsed onto `_read_node_json`; sealed-node handling uses a typed
`NodeSealedError` instead of exception-message string matching.

**Bugs fixed:** `--json-input` skipped required-argument validation (regression
test added); `atomic_write_text_group`'s except-path deleted backups that had
just failed to restore (now `else`-scoped); broken `mdclaw.__all__`;
ineffective `_NODE_REQUIRED_TOOLS` monkeypatch in test_cli; the false
"boolean flags reject true/false" skill sentence; `bin/mdclaw` now binds
PKG_ROOT + PYTHONPATH into the SIF so container tools run the same source as
host-side native tools (previously the SIF's baked package — a version skew).

**Deliberately NOT done, with reasons:** guardrail registry kept at 257 codes
(the hint text is weak-agent scaffolding; measure before pruning);
`read_ancestor_final_step`'s three-state sentinel kept (tests use the omitted
form as real API — audit overcounted); `validate_node_execution_context`'s
`validate_conditions` param kept (None-collapse would change strictness for
callers passing `actual_conditions=None`); progress.json entry shape kept
(agent-facing via inspect_job; thinning it is a contract change — decide
separately); the three failure entry points kept (thin adapters, distinct call
shapes); `literature/` kept (skill-referenced and working); visualization
constants kept (audit wrongly called them dead — they are module-local, used).

---

## 2026-08-12 — Over-engineering audit: ~7.5–8k lines removable, 89 → ~78 tools

Four parallel audits (DAG/node core, CLI/dispatch, peripheral subsystems,
skills) over ~59k lines of Python + 4.3k lines of skills, looking only at
harness/plumbing complexity, not MD physics. Findings are an assessment;
nothing has been changed yet.

**Headline ratios.** 263 guardrail codes, 2 code branches anywhere that test a
code value; 89 tools dispatched by `fn(**kwargs)` behind ~2,244 lines of
CLI/registry/meta machinery; all 89 `TOOLS` entries are identity mappings;
`from mdclaw import *` raises (all 17 `__all__` names unbound).

**Dead or orphaned, highest confidence.** claim/release node-lease machinery
(~170 lines, zero production callers); `mdclaw/metal/` (whole package, zero
callers, consumer removed in 8a39b78); `research/structure_analysis.py` (694
lines, docstring cites a workflow phase that no longer exists);
the Methods-report half of `evidence/reporting.py` (~1,208 of 1,683 lines +
534-line citation inventory — zero output files across ~50 recorded benchmark
runs); alias tools (`download_structure`, `get_alphafold_structure`,
`setup/check_surrogate_backend`, `explain_failure`); write-only
`artifact_sha256` that hashes multi-GB trajectories inside node.lock with no
reader.

**Same-fact-N-times.** Tool names stated 3–4x per tool across
import/TOOLS/`__all__`; parent-type contract implemented twice (create_node
branches vs `_ALLOWED_PARENT_TYPES` table); progress.json has grown from
"thin index" into a node.json mirror with its own repair tool; in skills/,
ion policy ×6, platform preflight ×6, `guardrail-codes.md` (276 lines) is a
byte-level duplicate of what `hints[0]` already delivers at runtime.

**MCP ghost.** No MCP plumbing remains, but 11 files carry a false "integrates
with external MCP servers" docstring and `test_mcp_server.py` tests the
registry — misnaming propagated into CLAUDE.md/testing docs.

**Bugs found incidentally.** `--json-input` path skips required-argument
validation entirely; `mdclaw/__init__.py.__all__` fully broken;
skills/md-prepare/explicit-water.md:86 states boolean flags reject
`true`/`false` values (false — `_parse_cli_bool` accepts them, and
bioemu-sample instructs `--reconstruct-sidechains false`); inconsistent
node.lock/progress.lock ordering (latent deadlock shape, masked by
single-writer usage).

**Deliberately deferred.** The 263-code guardrail registry is the one place
where over-engineering may be load-bearing weak-agent scaffolding (the payload
is LLM-facing hint text). Decision: measure against benchmarks before pruning
to the ~40 referenced codes; don't drift.

Totals: CLI/dispatch ~1,000–1,700; node/DAG ~1,150; peripherals ~4,040 py +
534 md; skills ~1,320–1,420 (61 files → ~35). Full per-finding detail with
line numbers lives in the session transcript of this date.

---

## 2026-08-12 — GPU verification: the CPU hour was an invocation defect

Follow-up to the fourteen-failure entry: the hour-long membrane equilibration
was not a property of the tests but of how I launched them. The SIF was
invoked without `--nv`, so the container had no CUDA platform and
`platform="auto"` silently fell back to CPU.

Verified three ways. Platform probes: without `--nv` the usable set is
`[Reference, CPU]`; with it, `[Reference, CPU, CUDA]`, and a 20k-particle
auto-selected Context lands on CUDA. Timing: the same membrane+metal chains
that took 1 h 08 m on CPU completed in **1 m 58 s** with `--nv` — about 35x —
with identical results (7 passed both ways). The production paths
(`bin/mdclaw`, the benchmark task wrappers) were never affected; they already
add `--nv` when `nvidia-smi` is present. Only hand-typed SIF commands
following the guide missed it, and the guide is fixed (`18ca28a`).

Corrected estimate: a full suite with the revived pipeline chains is ~45 min
with `--nv`, not the 1.5 h previously reported. The 3PWB chain deliberately
pins `platform="CPU"` for determinism and is unaffected.

Noted, not done: the executed platform lives only in tool results, not in
node.json metadata, so post-hoc provenance cannot say which platform produced
an artifact. Worth considering if platform ever becomes scientifically
relevant (e.g. mixed-precision differences).

---

## 2026-08-11 — The fourteen failures: one real bug, thirteen stale fixtures

All fourteen pre-existing failures are fixed (`5eb0486`, `5363222`); the full
suite is green for the first time on record (1381 passed, 0 failed). None of
the tests were unnecessary — the question that prompted the investigation.

**The real bug** hid in plain sight for three weeks. The node-sealing change
(2026-07-16, c532626) made terminal node.json immutable but missed
`_register_preview_on_node`, which re-called `complete_node` on completed
nodes. Every post-hoc preview/review attachment on a finished node failed —
and the tests that would have caught it were failing for unrelated fixture
reasons, so the signal read as noise. That is the cost of tolerating a red
suite: real regressions become indistinguishable from stale tests. Attachments
now go through append-only `preview_registered` events, the resolvers read
them back, and a regression test pins the sealed-node render-then-review flow.

**The thirteen others** were fixtures asserting contracts the code had
deliberately outgrown: the parentless-node ban in study jobs, the candidates
layout, mandatory prep-time candidate selection, prep-owned hydrogen
completeness, a package-attr shadowing, an unverified writeFile-inventory pin
(the new membrane call site does restore long residue names — verified before
pinning), and a chemically impossible synthetic nucleic fixture, now generated
from pdbfixer template geometry against the force fields' terminal templates.

Two side finds from review: `test_split_molecules` was writing `split_N/`
directories into the repository checkout (two were committed; removed, output
now under tmp_path), and the first version of the event fix wrote events
nothing read — the reviewer's "writing is useless without a reader" catch led
to the resolver change that makes the flow actually work.

With the membrane/metal prepare steps unblocked, those chains run their full
MD legs (packmol packing through CPU equilibration) in-suite again, adding
roughly 1.5 h to a full run. That is the price of the coverage being real.

---

## 2026-08-09 — MDStudyBench extracted; the benchmark harness leaves mdclaw

MDStudyBench is now its own public repository,
<https://github.com/matsunagalab/MDStudyBench>, extracted with the same
copy-and-trim pattern as MDPrepBench and reviewed the same way before the first
commit (the review caught a missed `parents[2]`, an unconditional host
`import mdclaw`, a README claim that `MDCLAW_PYTHON` drives the scoring
delegate, a self-contradictory default CLI policy, and the spark1 profile
defaults surviving the copy — all fixed pre-publish; see that repo's memo).

Unlike the prep suite, `mdclaw` is a deliberate runtime dependency of its
confirmatory path: the runner executes MDClaw production nodes, snapshots the
installed `mdclaw` package as the attested adapter source, and resolves node
inputs through `mdclaw.node`. Scoring an existing submission needs only
openmm/mdtraj/numpy.

With both suites gone, this repository dropped `mdclaw/benchmark/` (~17k
lines), `tests/test_benchmark/`, `benchmarks/`, `docs/benchmark/`, and the
registry entry — 74 files, −36.8k lines. What deliberately stays is the
stage-record hook in `mdclaw/_cli.py` (`MDCLAW_BENCHMARK_HARNESS_LOG`): it is
now a cross-repository protocol both benchmark harnesses rely on, and its
stage vocabulary must not change silently. The layout the maintainer asked for
is three sibling checkouts: `mdclaw`, `MDPrepBench`, `MDStudyBench`.

**Pre-existing test failures catalogued during the removal** (fail identically
with the removal stashed; none are benchmark-related): three
`test_evidence_server` study-evidence report tests (missing prod `node.json`
in the fixture), three `test_visualization_server` node-registration tests,
two implicit-solvent `test_md_helpers` builds, `test_modxna_support` residue
mapping, `test_pdb_export_resname_guard` inventory pin, one prepare step in
each of the 3PWB/membrane/metal pipeline DAG tests, and one structure smoke
test. 14 in total against 1357 passing; they need their own investigation.

This memo stays as the historical record of the benchmark work done while the
suites lived here; new benchmark entries belong in the respective repos'
docs/memo.md.

---

## 2026-08-09 — MDPrepBench extracted to matsunagalab/MDPrepBench

MDPrepBench is now its own public repository,
<https://github.com/matsunagalab/MDPrepBench>, laid out as a sibling checkout
(`/home/yasu/tmp/MDPrepBench`). Fresh history, MIT, everything public — the
task contracts and truth references were already world-readable in this repo,
so openness was made deliberate rather than accidental. The extraction is
copy-and-trim: package `mdprepbench` is the harness minus the four study-only
modules, with `grounded_correct_v2` entry points raising NotImplementedError
pointing back here. All 337 tests pass there; CI runs lint, dataset
consistency, and a no-OpenMM test subset (verified in a bare venv).

A pre-publish external review caught five release blockers before the first
public commit, the worst being container-delegated scoring still invoking
`python -m mdclaw._cli` — it would have scored with whatever MDClaw the image
carried instead of the published code. Details in the new repo's docs/memo.md.

On this side, mdclaw dropped the prep dataset, prep-only tests/tools/docs, and
the prep-fixture-dependent tests whose coverage now lives in the new repo
(336 still pass). Kept: `mdclaw/benchmark` (MDStudyBench needs it),
`run_mdprepbench_all_agents.py` + `audit_mdprepbench_run.py` (the study batch
wrapper builds on them; canonical copies are in MDPrepBench), and
`validate_submission.py` / `package_submission.py`.

**Accepted risk, recorded deliberately:** the removal deletes tests for code
mdclaw still ships — the shared batch runner's execution/pass^k tests, the
public-export overwrite guards, the fabrication-policy scorer tests, and the
P18/P24 scorer regressions. Their coverage lives on, green, in the MDPrepBench
repository, and the harness here is feature-frozen until MDStudyBench leaves the
same way; restoring transitional copies was judged not worth the drift. The
review that flagged this (rightly calling the hybrid unsound as a permanent
state) also caught that `datasets.py` still defaulted to the deleted
`benchmarks/mdprepbench` — fixed to `benchmarks/mdstudybench` before commit —
and that a first trim pass had deleted the *study* tests too, because
`DATASET_DIR` substring-matched `STUDY_DATASET_DIR`; restored from HEAD and
re-trimmed with a lookbehind. Suite after all fixes: 441 passed.

MDStudyBench is planned to leave the same way. When it does, `mdclaw/benchmark`
and the remaining shared tools go with it, and the copy-and-trim pattern plus
the blocker list from this extraction are the template.

---

## 2026-08-05 — MDPrepBench reference bundles, and what 40/40 does not mean

codex (gpt-5.6-sol, xhigh) was run as the solver over all 40 tasks through
`run_benchmark_agent`, so it saw only the public export — never `task.json`, never
the deterministic checks. **All 40 scored 1.0**, no failures, ~4 h 15 m across
three shards, ~11 min per task, essentially no GPU.

Bundles total 1.78 GB and live outside git at `$MDPREPBENCH_WITNESS_DIR`:

```
<task_id>/submission/prepared_structure.pdb
<task_id>/submission/topology/{system.xml,topology.pdb,state.xml}
<task_id>/harness_execution.json
```

`benchmarks/tools/witness.py` records them into
`benchmarks/mdprepbench/witnesses/manifest.json` (per task: run id, provenance,
repository head, a hash over everything the scorer reads for that task, and a
hash per bundle file) and re-scores them on demand.

**What 40/40 establishes, and what it does not.** It establishes that every task
has at least one bundle this model, scaffold, and runtime can produce inside the
budget and that the current scorer accepts. It does *not* establish scientific
correctness beyond what the scorer checks, resistance to scorer-targeted
shortcuts, task difficulty, or pass@1 reliability — there is one observation per
task. The historical per-task means of 0.28–0.66 are not a comparison: they mix
models, scaffolds, code versions, and known instrumentation failures.

**A rule I had stated and have withdrawn.** I proposed treating a codex failure
as evidence to suspect the scorer. That is unsound: a failure warrants diagnosis,
not a presumption against the scorer. And the converse matters more here —
40/40 does not vindicate the scorer either, because an overly permissive scorer
produces 40/40 too. Positive fixtures cannot detect a weakened scorer; deleting a
check leaves every witness at 1.0. The negative fixtures remain the other half.

**Defects caught in review before commit**, all in the first draft of the tool:
scoring writes `normalized_submission/` and `score.json` *into* the bundle, and
hashing those would have produced a delayed false "drift" the artifacts never
caused; acceptance checked only `preparation == 1.0`, ignoring `status` and
`weighted_total`; `record` and `verify` returned 0 on skipped bundles, an unknown
`--task`, or an empty manifest; drift detection missed added files; a bare
`--task` meant "everything"; the contract hash covered only `task.json`, so
swapping one of the five private `truth/*.pdb` references would have gone
unnoticed; and `_scorer_revision()` shelled out to git, which the container does
not have, silently recording "unknown".

---

## 2026-08-05 — Artifacts versus harness evidence: the declaration was wrong

`dataset.json` declared `evaluation_unit: "submission_artifacts"`, and the
maintainer states an agent need not use MDClaw's DAG. But the prep tasks carry a
reject-level integrity check, `workflow_execution_recorded`, requiring a harness
execution record. Demonstrated on codex's P01 bundle, with the artifacts
unchanged between the two runs:

| submitted | preparation |
|---|---|
| artifacts alone | **0.0** (`harness execution record required but missing or empty`) |
| artifacts + `harness_execution.json` | 1.0 |

So a third party preparing a perfect system elsewhere and submitting the files
scores zero, which is not what "artifact-based" promises.

Resolved by **fixing the declaration, not the check**, after the maintainer
confirmed that requiring the harness is acceptable: a foreign agent can be
plugged in with `--agent-command` and still not touch MDClaw's MD tools, and
`mdclaw/benchmark/*.py` imports nothing from the MD side, so the harness is
separable in practice. `evaluation_unit` became
`harness_executed_preparation_bundle`, following MDStudyBench's existing
`runner_certified_study_bundle`; `agent_independent: true` stays, being accurate.
`environment_type: "artifact_only"` in `task_specs/defaults.json` — which is
exported into the *public* contract agents read — became
`harness_executed_artifacts`.

Scoring behaviour is unchanged, so historical scores stay comparable. The known
weakness is recorded in the dataset notes: harness evidence establishes
runner-executed provenance, not that the preparation was genuinely performed. The
check asks for one successful `min`-stage command with a measured walltime, which
a wrapper around a trivial command satisfies.

---

## 2026-08-05 — Correction: the `mdclaw-free` arm is not structurally blocked

I claimed that all 120 free-condition task instances scored exactly 0.00 and
suggested the integrity requirement blocked the arm by construction. Wrong on
both counts.

The 0.00 figure came from globbing `benchmark_runs/cond_*` and deciding the
condition from `_free_` appearing in the run name. Those runs all record
`tooling_condition: "unknown"`. The runs actually labelled `mdclaw-free` are four
others, and they score normally:

```
20260704_mdprepbench_pi_v2_pi          overall 0.5136   40 tasks
20260706_mdprepbench_pi_pi             overall 0.5470   40 tasks
haiku_sif_free_20260616_125805         overall 0.2585   25 tasks
pi_deepseek_sif_free_20260616_171959   overall 0.5714   25 tasks
```

The uniform zeros in the `cond_20260705_*` haiku runs are recorded as
`missing_raw_artifacts` — those agents produced nothing — not as an integrity
failure. This overturns the suggestion in the 2026-08-04 measurement entry that
the ablation's free baseline could not score.

---

## 2026-08-04 — Correction: five MDPrepBench tasks do ship reference data

The entry below claims "No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`". That was checked against `P01` alone and is wrong. Five tasks
carry a `truth/` directory:

```
P03_prep_ligand_pose_t4l_benzene    ligand_reference.pdb        105 KB
P18_prep_membrane_mixed_lipids      model_1_reference.pdb       124 KB
P19_prep_nmr_model_selection        model_5_reference.pdb        97 KB
P24_prep_biological_assembly        assembly_1_reference.pdb    317 KB
P28_prep_kinase_inhibitor_gaff_1iep ligand_pose_reference.pdb   184 KB
```

The conclusion still holds, because these are a different kind of artifact. They
are *input-side* references: coordinates used to check that the agent started
from the right thing — the fifth NMR model rather than the first, the biological
assembly rather than the asymmetric unit, the ligand in the deposited pose. They
say nothing about whether a finished, force-field-applied system is correct.

What is still missing is the *output* side: a stored `system.xml` /
`topology.pdb` / `state.xml` bundle for a task, whose purpose is to detect the
scorer breaking rather than to grade an agent. Zero tasks have one, and no task's
`scoring` references a stored bundle (`ground_truth_checks` is `[]` for P01, and
no task.json mentions a reference or golden file).

| | existing `truth/*.pdb` | the reference bundle still wanted |
|---|---|---|
| stores | starting coordinates | the finished, parameterised system |
| detects | agent picked the wrong input | **the scorer itself regressed** |
| size | 100–300 KB | ~35 MB (P01, measured) |
| coverage | 5 tasks | none |

---

## 2026-08-04 — Retiring MDStudyBench S02-S04, and what the review changed

**Commit:** `8399dc6` (32 files, +55 / −2159)

Deleted `S01_stability_t4l_l99a` (referenced from nowhere in `dataset.json`, yet
still holding its prompt, task spec, and held-out truth on disk while sharing the
`S01_` prefix with the live task), the `S02`–`S04` extended tier, and the
fixtures for the v0.3 comparative-study construct they were the only users of:
`test_study_scoring_fabrication.py` (162 lines), `_fake_study_submissions.py`
(591 lines), and a scoring test asserting the agent must submit its own
comparative trajectories — the v2 contract has the runner own those.

**Reversed mid-change.** The first draft also deleted the `execution` and
`evidence_communication` score axes, which are used by no live task
(`execution` was non-null in 0 of 87 historical runs). A codex review pointed out
that those axes live in **MDPrepBench's** schemas and in the shape of every run
summary, so removing them would change the target suite's artifacts — and make
new summaries structurally incomparable to the 83 historical runs — purely to
finish MDStudyBench housekeeping. Reverted. The axes stay.

The LLM judge was also left alone. No shipped task declares `llm_judge_rubrics`
any more, so it has no scoring consumer, but the legacy study-scoring path is
interleaved with generic path validation, OpenMM rescans, and status handling.
Cutting it belongs in its own change, end to end, if it happens at all. The judge
tests now build a synthetic rubric task rather than referencing a deleted one.

---

## 2026-08-04 — MDPrepBench: measuring before proposing

Aggregated 83 historical runs from `benchmark_runs/*/summary.json`.

**Task quality is fine.** Every one of the 40 tasks has scored
`weighted_total = 1.00` at least once. Per-task mean ranges 0.28
(`P18_prep_membrane_mixed_lipids`) to 0.66 (`P17_prep_dna_duplex_neutralization`);
the fraction of runs at ≥ 0.8 ranges 26% to 69%. No unsolvable or broken task.
This overturns an earlier note claiming P18 fails for all models — true of the
model set at the time, not of the 54 runs now on record.

**Failure attribution — first answer was wrong.** 426 recorded task failures:
392 `missing_raw_artifacts`, 22 `invalid_openmm_bundle` (a known operator
environment misconfiguration), 10 `incomplete_running_work`, 2
`background_processes`. Inspecting 311 of the `missing_raw_artifacts` cases for
whether the agent had produced `topology.pdb` / `system.xml` / `state.xml` /
`minimized.pdb` anywhere under `work/` gave 310 "produced nothing", which was
reported as "essentially all failures are genuine capability failures".

That was wrong. It checked only for artifacts, never whether the agent process
ran at all. Adding exit code and tool-call records:

| classification | count | |
|---|---|---|
| zero tool calls recorded (start-up / infra suspect) | 253 | 81% |
| timed out (exit 124) | 48 | 15% |
| ran tools, produced nothing (genuine capability failure) | 9 | 3% |
| produced artifacts, failed to submit | 1 | 0% |

Those 253 concentrate in **10 runs**; one run has all 40 tasks failing that way.

**But do not over-correct either.** The harness log records only MDClaw CLI
calls, and in the `mdclaw-free` condition the agent is instructed not to use the
CLI, so zero tool calls is expected there and is not evidence of a start-up
failure. Seven of those ten runs are `cond_20260705_*_claude_code_*` ablation
runs. The honest reading is: the earlier "100% capability failure" claim is
definitely wrong; failures concentrate at the run level, which is a poor signal
for per-task capability; and only 9 cases are demonstrated capability failures.

**Consequence for the ablation.** MDPrepBench's distinguishing purpose is the
`mdclaw-free` / `mdclaw-cli-only` / `mdclaw-skills+cli` ablation. Zero-call does
not mean the same thing across those conditions, and the CLI-usage log was
separately shown to be silently discarded under the SIF runtime (see below). The
recorded conclusion — "the skill is the active ingredient, CLI alone ≈ free" —
should be treated as an observation under nominal conditions, not a causal
result, until treatment fidelity is verified per episode.

**Reference bundles.** No task ships one; `tasks/<id>/` holds only `prompt.md`
and `task.json`. Rather than promote a historical 1.00 submission (which shares
assumptions with the scorer that produced the score), witnesses are being
generated by running codex as a solver through the normal harness, which exposes
only the public export. First result: `P01_prep_simple_monomer_t4l`,
`overall_score = 1.0`.

---

## 2026-08-03 — Singularity inside a user namespace

**Commit:** `2699d45`

An agent working in another checkout wrapped `singularity` in `unshare -Ur`
after hitting the `unknown userid` warning, and every SIF invocation became a
full 5.1 GB extraction. Reproduced on this host, so it is not account-specific:

| invocation | elapsed |
|---|---|
| `singularity exec mdclaw.sif …` | 0.80 s |
| `singularity exec --no-home --bind "$PWD:/work" --pwd /work …` | 0.36 s |
| `unshare -Ur singularity exec …` | 65.7 s + 5.1 GB scratch churn |

A user namespace makes the kernel ignore the setuid bit on `starter-suid` and on
`fusermount3`, because the files' owner is unmapped there (`unshare -Ur` maps
only the caller: `uid_map = 0 37014 1`). Singularity falls back to FUSE, that
fails with `Operation not permitted`, and it extracts the image instead.

Floyd's accounts come from NIS (`nsswitch.conf: passwd: compat nis`, server
`crab`), which is why the lookup warning appears at all — but it is a warning,
not a failure. The guide's old wording, "avoid host account lookup by binding
the checkout at a neutral path", was read as "use a neutral UID". Reworded, and
`bin/mdclaw` now warns on stderr when it is about to launch Singularity from
inside a user namespace.

---

## 2026-08-03 — Conditions the certified adapter cannot honour

**Commit:** `9cdf91e`

A GPU run of MDStudyBench S01 in another checkout failed with
`condition_unverifiable` on every node, after spending 1 h 55 m on topology,
minimisation, and equilibration.

A declared node condition is a contract `run_production` must cross-check
(`mdclaw/node/lifecycle.py`), but the certified confirmatory adapter passes only
`--job-dir`, `--node-id`, `--simulation-time-ns`, `--temperature-kelvin`,
`--pressure-bar`, and (since `3420bc7`) `--random-seed`. `run_production` reports
13 conditions. Anything it reports as `None` that the node declared fails closed.

The immediate cause was `random_seed`, fixed in `3420bc7` — physics-neutral, and
the S01 prompt explicitly allows seeds to differ, so it should always have been
forwarded. The other checkout simply had not pulled.

The structural fix in `9cdf91e` rejects `platform`, `device_index`, and
`custom_force` at **plan freeze**, where the agent can still repair the node,
instead of at node execution after the GPU budget is gone. Deliberately still
declarable: `hmr`, `timestep_fs`, `implicit_solvent`, `is_membrane` —
`production.py` resolves these from the topology *before* building
`actual_conditions`, so they do verify. An earlier claim that `hmr` was dangerous
to declare was wrong; it was inferred from function signature defaults without
reading the resolution order.

---

## 2026-07-28 — S01 blind run: the answer was wrong, the harness was worse

**Run:** `studyv04_opus_s01_7h` — claude-code / opus, skills+cli, 7 h budget,
GPUs 1 and 5, dataset copied to scratch with `time_limit_minutes: 420` and the
prompt's "24 hours" reworded to match.

Final gates:

```
valid_execution   = true
claim_supported   = true
truth_agreement   = false
grounded_correct  = false      result_class = "grounded_wrong"
```

The solver claimed `decreased_hydration`; the evaluator's own replay agreed with
the claim; held-out truth is `increased_hydration`.

**The failure is the agent's, and it diagnosed it itself.** All four replicas
started from one `start_state.xml` (identical sha256) in which four bulk waters
had been relocated into the cavity. So the runs measured mild expulsion from a
pre-wet pocket rather than equilibrium filling of a dry one, and the
replica-agreement check passed vacuously. The solver said so in its own report,
considered claiming `unresolved`, and decided that substituting its judgement for
the published adequacy rules would be redefining the contract. That reasoning is
sound, and `claim_supported = true` backs it.

**Two harness defects surfaced first, both fixed.**

`03e7383` — the task-local `mdclaw` wrapper mounts `source_root` read-only, but
the harness execution log lives under it (`benchmark_runs/<run>/tasks/<task>/`).
`_write_benchmark_harness_record` swallows write failures by design, so every CLI
execution record was silently dropped, which to the scorer is indistinguishable
from an agent that ran nothing. This was a same-day regression: until `6f01e45`
the bind was read-write. For MDPrepBench, whose integrity checks set
`require_harness_record`, that would turn an environment detail into a hard
scoring failure.

`4abffc3` — confirmatory production runs in the SIF, but the runner inspected the
resulting artifacts in its own interpreter. The runner venv has `openmm` but not
`mdtraj`, so `_inspect_openmm_artifacts` raised on import and the fail-closed
catch recorded `openmm_artifact_inspection_failed` for four runs whose MD was
clean (adapter exit 0, no timeout, 1,250,000 steps and 206 MB trajectory each).
That zeroed `valid_execution` for a property of the operator's environment.
Inspection now delegates to the same container as the adapter, and a missing
container runtime yields `openmm_artifact_inspection_unavailable` rather than the
artifact-trust code.

**Salvage.** Re-inspecting the four completed nodes with the fixed code returned
`valid=True`, empty reason codes, and full runtime facts in ~17 s per node. The
episode was amended by merging only the inspection-derived fields — `runtime`,
`reason_codes`, `diagnostic_reason_codes`, `valid`, `attestation_scope` — while
keeping the runner's timings, adapter results, frozen plan, and artifact
snapshots, with a guard that aborts if live artifact hashes no longer match the
custodied snapshots. `attestation_scope` was missed in the first attempt, which a
codex review caught: `grounded_v2` requires
`production_runtime_matches_frozen_base_system` to be `true`, and the un-merged
event still carried `false`, so the amendment would have failed
`event_runtime_scope_unattested`. An audit receipt records the original and
corrected episode hashes, the SIF hash, and the full fresh inspection output.

**How to report this number.** As a post-hoc infrastructure-corrected
calibration, not as a clean run. `--no-session-persistence` does not give the
resumed claim stage a clean slate: the solver's own analysis files from its
earlier continuations were still on disk. The official record for this run
remains `0.0 / invalid_execution`; the salvaged score lives in
`score.salvage.json`.

---

## Open questions

- Verify treatment fidelity per episode before trusting any ablation number:
  free sees neither skills nor CLI, cli-only sees CLI but not skills,
  skills+cli sees both with a pinned skill-bundle hash.
- Split the `cond_20260705_*` zero-call failures into condition-expected versus
  genuine start-up failure. The recorded ablation conclusion rests on those runs.
- Extend codex-generated witnesses to the suspicious families — membrane, metal,
  protonation. If codex fails one, suspect the scorer, not only the agent.
- pass^k reporting for K = 3. `--repeats` already exists in
  `benchmarks/tools/run_mdprepbench_all_agents.py`; nothing aggregates across
  repeats. Fix the definition of "pass" first — `P01`'s deterministic checks
  contain zero hard gates, so a gate-based definition is vacuous;
  `scores["preparation"] == 1.0` is the candidate.
- Whether to delete the LLM judge end to end, now that no task declares
  `llm_judge_rubrics`.
