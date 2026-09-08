# SMO報告を起点とする汎用的な化学状態・残基検査の修正計画

作成: 2026-09-08。実装前の計画。
根拠: [調査記録](../research/smo-20260908-bug-investigation.md) / [再現結果](../research/smo-20260908-audit.json)。

## 目標と修正後の契約

1. prepで確定した結合・プロトン化状態を、中和用Systemと最終Systemで共有する。
2. 要求を解決できない場合は、化学状態を変えず、原因を特定できるエラーで停止する。
3. 残基の存在、名前、配列長、選択結果、実際のSystemの化学状態を区別して報告する。
4. 成功判定を結合の本数や名前だけに依存させず、要求した原子対と力場の状態で検証する。
5. SMOの成功に加えて、下記の非SMO回帰行列を合格条件とし、既存の正常系を壊さない。

Pablo全廃、新しい脂質ビルダー、MDエンジン変更は今回の修正に必要ない。
既存ユーザの終端ノードを上書きせず、調査で見つかった不整合を勝手に修復しない。
未確認の「報告者が何を読んだか」を前提に実装しない。

## 1. 結合計画と実行を統一する（最優先）

主対象: `amber/topology_bonds.py`、`amber/build_system.py`、
`amber/openmm_build.py`、`_topology_pablo.py`、既存の残基識別ヘルパー。

- 入力形式の互換性を保った正規化・解決処理を一つにする。
  chain/resnum/insertion codeだけでなく、必要なcomponent/chain identity情報を用い、
  膜中の重複したchain/resnumを誤って同一視しない。
- 「要求されたペア」「構造上で解決したペア」「既存/追加の結合」を別々に記録する。
  結合追加関数は解決済みペアだけを受け、未加工の引数を再解釈しない。
- HG付きCYSへのS–S要求、存在しない/曖昧な部位、競合する相手は、System生成前に失敗させる。
  `skipped_cys_protonated` と表示しつつ実行する現状を廃止する。
  topoで無断にHGを除去したり、ユーザの要求を黙ってスキップしたりしない。
- 失敗には安定したcode、対象部位、観測された状態、prepの新規枝での修正案を返す。
  code名は既存guardrail登録を確認して決め、CLI・trace_failure・skillで統一する。
- None（未指定）と空リスト（明示的に結合なし）の意味を保持する。
  PDBの既存結合と明示指定が衝突した場合も、無条件の距離再推定で解決しない。
- 既存の正しい結合は重複追加せず、同じ計画に対して処理結果が安定するようにする。

完了条件: topo_004相当のHG付き入力は、指定ありなら説明可能な事前エラー、
指定なしなら還元状態を維持して成功。正しいCYX入力は9本の要求をその相手のまま満たす。

## 2. 中和用Systemへの化学情報の引き継ぎを修正する

主対象: `node/inputs.py`、`solvation/membrane.py`、`solvation/patch_membrane.py`。
1の共通処理を利用する。

- solvの入力解決で、選択したprepの結合計画と必要なidentity mappingを取得する。
  ディレクトリ探索や別枝の「最新prep」から推測しない。
- orientation/assembly後の残基に計画を明示的に対応付ける。
  座標回転や水・脂質追加を経ても、同じタンパク質部位の計画であることを検証する。
- `_compute_membrane_net_charge` に計画を渡し、本番ビルドと同じ結合検証を通す。
  中和用の一時ビルドはsolv内の処理として扱い、ユーザDAGに偽のtopo完了を作らない。
- standalone入口を保持する場合は結合指定を明示的に受けられるようにする。
  CYXがあるのに結合を解決できない場合、距離だけで推測して中和を進めない。
- 電荷は既存のNonbondedForce合計を使う。力場・水モデル・対応している化学情報を
  中和と本番で一致させる。新しい残基名ヒューリスティックは追加しない。
- NonbondedForceがない、非有限値、整数から許容誤差以上にずれるケースは失敗させる。
  無条件の0初期値やroundで異常を隠さない。
- 電荷の生値、計画の出所、ペア検証結果、配置イオン数、配置後の電荷収支を保存する。
  本番topoの既存 `neutralization_charge_mismatch` 検証も維持する。
  同じ巨大Systemの不要な再構築を増やさず、イオン置換直後は電荷収支、最終topoは
  実Systemの電荷を使って検証する。

完了条件: SMOの中和用ビルドが+2e・9本のS–Sを返し、Cl−Na=2のイオン配置になる。
標準状態の別枝はその枝の正しい電荷を保ち、元の+2へ強制しない。

## 3. 化学状態と入力保存性の検証を強化する

主対象: `amber/topology_validation.py`、System生成時のtemplate適用・検証箇所。
1と同時に最小ガードを実装し、2の受け入れ条件にする。

- S–S検証を「観測本数が期待以上」から、解決した原子対の照合に変える。
  TopologyとSystem双方で要求ペアを検証し、同じ本数で違う相手を結ぶケースを拒否する。
  Systemの結合表現（力項/対応するconstraint）も扱う。
- 明示計画の有無にかかわらず、各CYXに正しいSG–SG接続とHG不在を要求する。
  HG付きCYSにS–Sがあるケースも拒否する。
- CYXという表示名でCYM相当の電荷が割り当てられないよう、適用templateと化学状態を照合する。
  必要に応じて既存residueTemplates機構で対応templateを固定する。
  template名は力場・端末状態に依存するため、全CYXの電荷を単純に0と固定しない。
  末端電荷を含む正しいtemplateとの一致を検証する。
- HID/HIE/HIP、ASH/GLH、LYN/CYMも、必要なHパターン・識別子の検証を維持する。
  insertion codeを最終検証まで一貫して使う。
- 元入力との原子/残基保存性を、出力Topology/System/Stateの相互一致とは別に報告する。
  virtual site追加等の許可された変換は個別に記録し、単なる総原子数一致で代用しない。
- 実際のloader、fallback理由、入力/出力・計画の識別情報を構造化結果に残す。
  既存フィールドの意味を変更せず、新しい検証フィールドとして追加する。

完了条件: これまで「CYX、21/21 passed」となった負電荷の孤立SGが成功しない。
同数の間違った結合、残基欠落、別のinsertion-code部位との取り違えを検出できる。

## 4. 残基分類と配列生成を共通化する

主対象: `chemistry_constants.py` と必要な共通ヘルパー、
`research/inspection.py`、`structure/split.py`。

- 公開inspectとprep/split内部で、同じ残基分類と一文字配列への変換を使う。
  AMBER別名を標準アミノ酸の基底名に対応させ、配列長から落とさない。
- 定義集合だけを差し替える局所修正で終わらせず、重複したAA_CODEや分類ロジックを整理する。
  大きなinspect関数全体の統合は不要。意味がずれた共通部分を抽出する。
- amino-acid sequence length、全残基数、cap数、未知/修飾残基の扱いを定義する。
  ACE/NMEを含む場合まで「配列長=全残基数」と決めつけない。
  対応外の修飾を黙って除外せず、既存のPTM・nucleic・glycan分類との整合を確認する。
- 実構造の原子・残基名は表示の都合で変更しない。
- 既存JSON構造をできるだけ維持し、誤っていた値を正す。

完了条件: SMOの3入力すべてで公開inspect、splitの配列長、抽出PDBが476で一致。
AMBER別名だけの短いタンパク質もligandと誤分類しない。

## 5. skillと診断結果の案内を更新する

実装したcodeと検証フィールドが確定した後に実施。
`skills/` を正本として編集し、mirrorの整合を検証する。

- 共通の異常診断leafを一つ設け、md-prepare/md-analyze/md-reportから条件付きで参照する。
  通常のspineを長いチェックリストにしない。
- 「残基が消えた」「S–Sがない」「電荷が違う」と疑われた場合に、対象nodeとファイル、
  実loader、入力対応、検証原子対/電荷、選択式、ツール結果を順に確認させる。
- sequence_length、residue count、MDTraj/PyMOL selection、PDB別名の正規化は
  別の概念であることを明示する。
- explicit-waterのビルド説明にPDBFile fallbackと既存脂質template補完を記載する。
  ソースを読んだ推測より、実行時の構造化loader情報を優先する。
- 化学的要求を変えて成功させることを修正と呼ばない。
  HG付きCYSの修正はprepの新しい枝で行い、通常のDAG手順を示す。
- 手動介入がある履歴では現在のcompletedだけを根拠にせず、イベント、tool_result、
  実Systemを照合する。矛盾した履歴を自動上書きする機能は今回追加しない。
- 誤りを報告する際は、コマンド・対象ファイル・観測値・期待値・実行版を残し、
  観測と原因推定を区別する。stdout JSONは保存し、grepの抜粋だけを根拠にしない。

完了条件: 実際の新しいCLI出力を使った手順確認で、今回の3症状を正しいcode/証拠へ誘導できる。
CLI引数変更がある場合、tool-reference、--list-json、CLI入力検証と例を同時に更新する。

## 検証とリリース順序

実装を4つのレビュー可能な単位に分ける。

1. 結合計画の統一＋対応する化学検証（1、3の必須部分）。
2. 中和への引き継ぎ＋最終電荷検証（2、3の残り）。
3. 残基分類・配列生成の共通化（4）。
4. skill・診断資料・リリース説明（5）。

各単位で既存テストを拡張し、新規テストは失敗した契約を検証する。

- 小さいローカルfixture: 長いSG間距離の明示ペア、HG付きCYS、孤立CYX、CYM、
  insertion code、重複chain/resnum、同数で別相手の結合、空指定、HG不在の正しいCYX。
- DAG結合試験: 正しいprep枝からsolv→中和用ビルド→topoへ計画とIDが保持されること。
- 検査整合試験: 公開CLIと内部splitのAMBER別名、caps、PTM、nucleic/ligandの分類。
- 既存disulfide/variant/topology、solvation、chain-selection、CLI/registry/guardrailテスト。
  node入力解決を変えた場合は専用resolverテストを必須にする。
- SIF-overlayで実依存関係を用いたsmoke/pipeline試験。OpenMM経路には--nvを付ける。
- 最後にSMO実入力の新しい隔離studyで、元ファイルを変更せずprep由来計画を用いて
  solv→topo→短いminを確認。476残基、9組の正しいS–S、主鎖切断0、全電荷0、有限エネルギー。
  9/3のHG付き入力は事前エラーになることも確認する。
  同一の巨大系を単体テストとして常時実行せず、合成fixtureと実ケース検証を分ける。
- 既存pipelineで脂質の結合補完、標準状態/非膜系が退行していないことを確認する。
  下記のユーザ指定による同一SMO系2.1 ns平衡化試験を、最終受け入れの必須条件とする。
  それ以降のproductionは今回の試験に含めない。

Python/skillの開発中は既存SIFにcheckoutを重ねる。配布時はrelease/container手順で
更新コードを含むSIFと版・manifestを整合させ、SIF単体のimport先と修正ファイルのhashを確認する。
ユーザの既存runを自動置換せず、新規枝での再準備・再検証が必要な条件を案内する。

## 必須実行試験: 報告と同じSMO系で修正完了を確認する

ユーザの追加指示により、合成fixtureや短いminだけで修正完了としない。
以下の固定入力による対照試験と、通常CLIでの膜構築から2.1 ns平衡化までを必須とする。
これは実行計画であり、本計画の作成時点では新たな計算を開始しない。

### 固定する入力と比較対象

元データのルート:
`/data1/rkp00079/rku00140/structures1/SMO_WT_active_6XBL/study/jobs/main/nodes/`

| 用途 | 固定するファイル/ノード |
|---|---|
| 同じ受容体座標・化学状態 | prep_008/artifacts/merge/merged.pdb |
| 要求する9組のS–S | prep_008/artifacts/disulfide_bonds.json |
| canonical component識別 | prep_008/artifacts/chain_identity_map.jsonと関連prep artifact |
| 元の膜構築条件/配置 | solv_005のmembrane_metadata、patch metadata、box/geometry metadata |
| 元の未修正膜 | solv_005/artifacts/membrane.pdb |
| 正常な通常topoの参考値 | topo_005のSystem/Topology/State |
| HG付きCYSの既知失敗 | topo_004/artifacts/system.prepared.pdbとamber_metadata内の9組 |
| ユーザ迂回結果との比較 | topo_007、min_002、eq_001（読み取りのみ） |

開始時に全入力のSHA256、修正前/修正後commit、SIF、依存版、import先をmanifestへ記録する。
同じPDB名だけで同じ入力とみなさない。実際にimportされた修正ファイルのhashも照合する。

独立したテストstudy/jobを作り、既存テストfixture方式で元の完了source/prepと必要な
provenanceを読み取りコピーしたベースラインを用意する。コピー由来と元hashを明示し、
prepを今回再実行したとは記録しない。これによりMODELLER等の再実行による座標差を避ける。
新しいsolv/topo/min/eqのみ通常のnode API/CLIで作成する。
コピー後の参照先とDAGをinspect/explainで検証し、ユーザ領域に書き戻す経路を禁止する。
元のrun中のproduction、終端node、共有キャッシュは変更しない。

### 試験A: 固定構造の修正前後対照（化学状態の原因を切り分ける）

1. 同じsolv_005入力からイオンのみを除いたPDBを作り、変換記録を残す。
   タンパク質・脂質・水の座標は変えない。
2. 修正前コードの対照は、記録済みの「結合計画なし→8本・電荷0」と
   「計画あり→9本・電荷+2」を使用し、manifestが一致しなければ再計測する。
3. 修正後の中和用ビルドがprepの計画を受け取り、**9組の要求結合・電荷+2**になることを確認。
4. 孤立CYXを残した計画欠落入力では、電荷0として成功せず、構造化エラーになることを確認。
5. topo_004のHG付きCYS + 9組指定は、System生成前の新しいエラーで停止することを確認。
   同じ入力の空指定は還元状態として成功し、正しいCYX + 9組は酸化状態として成功する。
6. 同じ3つの保存PDBで公開inspectとsplitを実行し、配列長がすべて476、抽出PDBも476であることを確認。

合格条件: 原因ごとの正常・異常入力が期待した結果になり、単に最終successだけで判定しない。

### 試験B: 通常の膜構築から最小化まで（必須）

テストjobの固定prepを親に、以下を実行する。

```text
create/explain solv → embed_in_membrane
create/explain topo → build_amber_system
create/explain min  → run_minimization
```

- 実入力はprep_008。受容体476残基、HID×8、CYX×18、ASH×1、GLH×2と9組のS–Sを固定。
- 脂質POPC、ff19SB/OPC、NaCl 0.15 M、patch-tile。
  元のpatch cache keyは `92fd031d235d13c1aa47f9a8b054212099d68d798d916eea475d6243d213a65a`。
  可能なら同じパッチをテスト用キャッシュにコピーし、座標/構成のhashを照合する。
- 元のorientation、dist/dist_wat、salt、box等の保存条件を実行前に表へ確定する。
  不足している設定を「元と同じ」と推測しない。既存の向き付き中間構造を使う場合は
  その範囲を明記し、結合計画のID受け渡しは通常のsolv経路で検証する。
- 中和は修正版embed_in_membraneに実行させる。solv_006の手動イオン修正や
  direct_openmm_testの迂回スクリプトは使わない。
- ユーザの比較対象とそろえ、topoはhmr=False、minはsolute_heavy拘束、
  拘束定数100（CLIの既定単位を署名・保存metadataで確認）、max_iterations=5000。
  topo/minの状態ファイルを後段へ通常のDAG解決で渡す。

段階ごとの必須検査:

| 段階 | 検査内容・合格条件 |
|---|---|
| prep→solv | 476残基と入力重原子の対応が保持され、化学状態と9組の計画が不変 |
| 中和用System | 正しい9組のS–S、電荷+2e（数値許容誤差1e-3e） |
| イオン配置 | Cl数−Na数=2。水/イオン置換数と電荷収支が記録と一致 |
| topo | Topology/System/Stateが整合、9組を原子対で照合、全電荷の絶対値<1e-3e、主鎖切断0 |
| lipid | 元と同じ脂質構成。内部/外部結合、未接続の外部結合原子0を確認 |
| min | エネルギー・座標が有限、要求構造と結合を維持。SG間距離と主鎖C–N距離を直接集計 |

元の構成・配置を完全固定できる試験Aでは原子数の一致を厳密に要求する。
膜を再配置する試験Bでは水/イオン数が変わり得るため、157616という総数だけを成功条件にしない。
タンパク質・脂質の対応と許可した水/イオン差分を必ず説明する。

### 試験C: 元の報告と同条件の平衡化（必須）

試験Bのminが合格したら、そのまま通常の `run_equilibration` で進める。
保存されたeq_001に合わせて、以下を固定する。

- NVT: **0.1 ns / 50000 steps**。
- NPT: **2.0 ns / 1000000 steps**。
- **300 K、1 bar、2 fs、hmr=False**。
- 膜用のsemi-isotropic設定をSystem/実行metadataで確認。
- solute_heavy拘束と膜headgroup拘束は元の設定を確認して同じものを使う。
  元の観測数はprotein heavy 3738、lipid headgroup 221。数だけでなく選択部位も比較する。

NVT終了時、NPT終了時、保存された軌道フレームで、残基/重原子対応、
S–S原子対、PBCを考慮したSG–SG/C–N距離、有限性を独立に確認する。
温度・体積・密度は時系列と末尾10%の統計を保存し、元のeq_001と並べる。
乱数軌道、最終エネルギー、体積収縮率が以前の値と完全一致することは要求しない。

- 原子・結合の消失、主鎖切断、NaN/Inf、未完了の時間、実行条件の不一致は不合格。
- 数値的に完走しても、温度・密度・膜構造に異常がある場合は保留として原因を調べる。
- 温度・密度・結合距離の数値判定範囲は元のeq_001の実統計と既存検証規約から
  **実行前に**試験仕様へ固定する。実行後に都合よく閾値を動かさない。
- 最後に同じ視点の膜全体/上面画像を作り、明らかな配置事故がないか補助確認する。
  画像を結合・電荷の検証の代用にはしない。

### 実行環境・成果物・完了条件

- 本番に近いGPUノードを使用。実行時にhpc-runの手順で資源を確認してSlurmへ投入する。
  ジョブスクリプト・stdout/stderr・実測時間・GPU利用を保存する。
- 初回は再利用patchを前提に1 GPU、walltime 60分を見込む計画とし、投入前に
  利用可能GPUと既存ログから妥当性を確認する。これは実行時間の保証ではない。
  timeoutや資源不足は科学的合格と扱わず、通常の再開/新規枝の契約に従う。
- 出力study_plan、node/event、manifest、構造化検査JSON、比較表、時系列図、
  failure対照結果を一つの試験レポートにまとめる。
- リリースSIFを更新した後、SIF単体のCLI・import/hashと固定構造の試験Aを再確認する。
  overlayだけ通って配布版が古い、という状態では完了としない。
- **A/B/Cがすべて合格し、通常CLIで中和→構築→min→2.1 ns eqまで成功した証拠が揃って初めて、
  「同じSMO系で修正を確認した」と報告する。製品全体の修正完了・配布判定には、
  下記の非SMO回帰試験の合格も必要とする。**
- いずれかが未実行/失敗/条件不一致なら、その事実を明記し、修正完了とは宣言しない。

## SMOへの過適合を防ぐ必須設計・試験条件

SMOは既知不具合の受け入れケースであり、仕様の定義元ではない。
以降は各実装単位とリリースの必須条件とする。SMOが通っても、別の正常系を壊せば不合格。

### 実装へ入れてはいけないSMO依存

- PDB ID、残基番号490/507、chain A、476残基、9本、+2e、POPC/OPC、特定のcache keyやパスを
  判定・修復分岐に埋め込まない。これらはSMO fixtureの期待値に限定する。
- 検出距離をSMOの長いSG間距離に合わせて広げない。
  明示結合計画を優先し、金属配位の硫黄をS–Sへ誤変換しない。
- CYMをCYXへ自動変換しない。正当な還元CYS、自由thiolate、金属配位CYMを保持する。
- 「CYXは常に0e」「全系は常に中性」「protein選択は全残基を含む」を共通仕様にしない。
  末端、caps、選択した力場、neutralization intent、金属/リガンド/核酸/荷電脂質を考慮する。
- namesや部分電荷の手修正でSystemを合わせない。化学状態と適用templateの整合を確認する。
- 新しい検証が扱えない系を、汎用エラーや黙った削除で通過させない。
  既存の対応範囲を狭める変更が必要になった場合は、具体的な系・理由・移行経路をレビューに出す。

### 共通の正しさを決める根拠

- 期待する結合は明示計画/既存の検証済み構造、電荷は選択した力場のtemplate・System、
  残基の存在は入力のatom/residue inventoryに求める。
- 期待値を修正対象の検査関数で生成しない。fixtureの独立した原子対一覧、
  生PDBの列読み取り、System XMLの独立集計と比較する。
- 既存の正常系では、旧版と新版を同じ入力・乱数・計算設定で比較する。
  原子対応、結合相手、template/電荷、粒子数、box/force classを比較し、
  同一状態のエネルギー/力も既存の数値許容誤差で確認する。
  修正に伴う正当な差分は原因ごとに説明する。失敗を消すためだけのgolden更新は禁止する。
- 旧版も誤っていたケースでは旧出力を正解にせず、独立の化学的期待値を用いる。

### 必須の非SMO試験行列

全組合せを巨大MDで実行せず、共通ヘルパーの小さい試験、実依存関係のSystem構築、
代表系の短いDAG実行に分ける。膜+金属、膜+プロトン化変異、ID衝突など、
今回変更した境界をまたぐ組合せは明示的に含める。

| 分類 | 固定する代表ケース | 守る契約 | 必須の試験深度 |
|---|---|---|---|
| S–Sの正例 | tests/data/disulfide_bpti.pdb（BPTI） | SMOと異なる番号・3組の正しいS–S | 検出/計画/System構築＋非膜solv→topo→min→短いeq |
| S–Sなし | 既存の小さいCYS含有・還元状態fixture、S–Sを持たない非SMO膜fixture | 自動的に結合を増やさず、CYS/HGを保持 | 正常系差分比較＋System構築 |
| 金属配位 | metal_site_cys4.pdb、既存metal pipeline | 接近したSGでも金属配位を壊さず、CYMを保持 | 金属guard＋System/pipeline、膜へのhandoff小型試験 |
| プロトン化 | HID/HIE/HIP、ASH/GLH、LYN、CYMを含む小型fixture | Hパターン・template・電荷・配列長 | variant別パラメータ試験＋実System |
| 末端/caps | N/C末端CYS・CYX、ACE/NME、複数chain | 末端電荷とcapsを誤判定しない | template照合/分類/結合検証 |
| 非SMO膜 | 既存2LOP膜pipeline | 別構造でも中和・lipid接続・DAGを維持 | embed→topo→min→短いeq |
| 膜組成 | 対応済みPOPC、POPC/POPE/CHL1混合、catalogで対応確認した荷電脂質 | リピド構成と総電荷を正しく含める | 小型膜System＋イオン収支 |
| 膜表現/経路 | lipid21 modular/full、patch-tile/既存packmol-memgen、Pablo成功/PDBFile fallback | loader/表現を変えても同じ化学契約 | 既存経路の実依存smoke＋境界試験 |
| 力場/水 | 対応済みff19SB/OPC、ff14SB/TIP3P | ff19SB/OPC専用の修正にしない | 小型System/solvationの両ペア |
| 非タンパク質 | 既存の標準DNA/RNA、GLYCAM糖タンパク、リン酸化、荷電ligand/標準ionのpipeline | 別名対応でligandやglycanをproteinと誤分類しない | 既存pipeline＋分類試験 |
| 中和意図 | 正/負/0電荷、保持ionあり/なし、salt=0、neutralization無効、explicit/implicit/vacuum | 全系への強制中性化やイオン二重計上をしない | 条件表テスト＋代表実System |
| ID・入力形式 | 複数chain、番号の重複、insertion code、PDB/mmCIF、未指定/空/重複/競合ペア | 相手を取り違えず、不明な要求は説明可能に失敗 | resolver/CLI/System試験 |
| 実行設定 | HMRあり/なし、対応するconstraint設定 | 質量変更と結合検証を混同しない | System/短いrestart smoke |

既存の `test_pipeline_*`、`test_disulfide_metal_guard.py`、
`test_disulfide_insertion_codes.py`、`test_amber_variant_restore.py`、
`test_lipid21_xml_selection.py`、`test_solvation_server.py`、`test_chain_selection.py` を再利用・拡張する。
表中の「対応済み」は現行catalog/既存試験で確認してfixture化し、今回の修正に合わせて
新しい科学的対応範囲を増やすことはしない。

### IDや表現を変えても答えが変わらないことを検証する

固定seedで変形したfixtureを作り、元との対応を使って以下を確かめる。

- chain名変更、residue番号のオフセット、atom/chainの並べ替えでも、結合相手と電荷が不変。
- 同じ入力に脂質/水/ionを追加し、蛋白質とchain/resnumが衝突しても、対象を取り違えない。
- 全体の適切な剛体回転・並進やorientation処理を経ても、計画の意味が不変。
- 同じ化学状態の正当な別名/PDB round-tripでは、残基の存在と化学的意味が不変。
  HIP↔HIDやCYX↔CYSのような実際の化学状態変更まで「同じ」とは扱わない。
- 同じ計画の再適用で結合を重複追加しない。
- ユーザの結合指定が部分指定か完全指定かを既存契約で区別し、
  単純な「総本数=指定数」だけで正当な既存結合を拒否しない。
  要求ペアの一致と、既存の追加ペアの正当性を別々に評価する。

### 実行順とリリースゲート

1. **実装前**に既存正常系のbaselineと非SMOのfixture一覧・期待値・許容誤差を固定する。
   代表例の選定理由は変更した境界の被覆とし、修正後に通った系だけを選ばない。
2. 各実装単位で小型fixtureと既存テストを実行する。
   変更対象と無関係な「期待値の書き換え」で退行を隠さない。
3. 実依存環境で行列の構築/pipeline試験を実行する。
   ネットワーク依存の構造は事前取得してhashを固定する。
   依存不足によるskipは合格と数えず、必要な環境で実行してから次へ進む。
4. BPTI系と非SMO膜系（既存2LOP fixture）の短いmin/eqを必須とする。
   explicit系の短いeqは原則10 ps NVT＋20 ps NPTとし、対応するforce field、水、HMR、
   restraint条件を実行前に固定する。短時間での密度収束は要求せず、
   実行・restart・化学的保存性の確認に使う。
5. 非SMOの必須検査が通った後に、固定SMOの2.1 ns平衡化試験を実行する。
6. リリースSIF単体でも、SMOの固定対照に加え、非SMOのS–S正例・金属負例・
   inspection CLI・別の力場/水のsmokeを実行し、overlayとの取り違えを排除する。
7. **SMOだけ成功ならリリース不可**。全必須ケースのpass/fail/skip、baselineとの差分、
   変更理由を表で公開する。新しい非SMO退行があれば原因修正まで完了扱いにしない。

SMOの2.1 ns試験の資源計画と、追加する非SMO試験の資源計画は別に積算する。
すべての系で長時間MDを走らせるのではなく、変更の影響を検出できる試験深度を選ぶ。
