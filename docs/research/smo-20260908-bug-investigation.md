# SMO膜構築バグ報告の調査 — 2026-09-08

対象: `/data1/rkp00079/rku00140/structures1/SMO_WT_active_6XBL/`。
以下では `nodes/` を同ディレクトリ内の `study/jobs/main/nodes/` とする。
ユーザのファイルは読み取りのみ。再構築は `/tmp/smo-bug-audit/` 内で実施し、
ユーザのDAG、実行中のproduction、インストールには変更を加えていない。
機械可読の集計は [smo-20260908-audit.json](smo-20260908-audit.json)。

## 結論

- Bug 1の「AMBER別名の残基がすべて消える」は、保存成果物と現行コードの再構築の双方で否定される。
  ただし報告者の解析スクリプト・9/8の独立した最小再現成果物は発見できておらず、
  報告時の別ファイルや別実行まで否定するものではない。
- Bug 2の中和電荷誤りは再現した。中和用の一時Systemに明示的S–S結合計画が渡らず、
  距離検出に失敗したCYXがCYM相当の負電荷でパラメータ化されるのが原因。
- Bug 3の「利用可能な入力状態がない」は現行コードでは再現しない。
  実入力のCYXとprepの結合リストを渡すと9本すべてがSystemに存在する。
- lipid21のPDBFile経路での結合補完は既に実装され、ユーザの通常ビルドでも使用されている。
- 今回に関係する科学計算コードは最新版と同一。古い科学計算実装の使用では説明できない。

## 実行版の確認

`git ls-remote origin refs/heads/main` で確認したupstreamは
`fce3dbcd14d0e4ab5e6c84a8275b5801580a2213`。
ユーザのチェックアウトHEADは `1689f13079920480eb87c08258cc63a004565c55`。
全141個のPythonファイルを内容比較したところ、upstreamとの差は
`mdclaw/slurm/_base.py` のみだった。

ユーザの `mdclaw_wide.sh` は
`/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif`
を指定し、ユーザのチェックアウトを `PYTHONPATH` に設定する。
同じ指定でのimport先は
`/data1/rkp00079/rku00140/mdclaw/mdclaw/__init__.py` だった。

チェックアウトを追加しないSIF単体も直接検査した。MDClaw配布版は0.6.8、
openff-pabloは0.2.2。SIF内の全141個のPythonファイルについても、upstreamとの差は
`slurm/_base.py` のみ。Amberビルド、Pabloブリッジ、膜構築、patch-tile、
clean_proteinのSHA256が一致することも確認した。
従って「mainの最新コミットそのもの」ではないが、今回の科学計算実装は最新と同じ。
過去の各コマンドの完全な起動環境はイベントに記録されておらず、遡及して断定できない。

## Bug 1: 実データには残基も結合も存在する

PDBのATOM/HETATM行を直接読み、N/CA/Cを持つ残基をタンパク質として集計。
名前の標準20種リストに依存せず、System XMLのHarmonicBondForceの原子インデックスで
隣接残基C–N結合とSG–SG結合を検査した。電荷はNonbondedForceを独立に合計した。

| 通常/迂回ビルド | 原子数 | タンパク質残基 | HID | CYX | ASH/GLH | System内S–S | 主鎖切断 | タンパク質電荷 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| topo_005（通常） | 157616 | 476 | 8 | 18 | 1/2 | 9 | 0 | +2 |
| topo_006（通常、別のprep） | 157628 | 476 | 8 | 0 | 0/0 | 0 | 0 | −1 |
| topo_007（ユーザ迂回） | 157616 | 476 | 0* | 0* | 0/0* | 9 | 0 | +2 |

*迂回出力ではOpenMMのPDB表記がHIS/CYS/ASP/GLUに正規化されている。
名前だけでプロトン化状態やS–S結合の消失を判断できない。
通常ビルドtopo_005には29個の別名残基が元の名前のまま存在する。

`topo_006` のS–Sが0本なのは `prep_009` 由来の別枝である。
prep_009のdisulfide_pairsは空であり、入力solv_007にもCYX/ASH/GLHはない。
HIDは入力・出力とも8個存在する。

標準20種類だけで残基を選ぶと、topo_005は447残基（476−29）、topo_006は468残基
（476−8）になり、報告された欠落数と一致する。残基選択による見かけ上の欠落が
有力だが、報告者の実際の解析コードがないので、原因確定とは区別する。
参考にMDTrajのis_proteinも検査したが475残基となり、この選択だけでは29残基欠落を再現しない。

保存された通常ビルドtopo_005/006のイベントは9/7、迂回ビルドtopo_007は9/8。
今回の新規再構築でも別名残基は保持され、両条件とも157452原子
（元の157616原子からNa/Clの164原子だけを除外）だった。

## Bug 2: 一時ビルドへのS–S計画の引き継ぎ漏れ

実際の経路は次のとおり。

1. `solvation/membrane.py` の `embed_in_membrane` がpatch-tileを選択。
2. `patch_membrane.py:2601` 付近で組み立て途中PDBを作り、net_charge_fnを呼ぶ。
3. `membrane.py:1231` の `_compute_membrane_net_charge` が `build_amber_system` を呼ぶ。
   この呼び出しにはjob/nodeも `disulfide_bonds` も渡されない。
4. 実際のNonbondedForceの部分電荷を合計してイオン数を決める。
   残基名による形式電荷ヒューリスティックではない。

`solv_005/artifacts/membrane.pdb` からNa/Clのみを除いた同一PDBを使い、
現行checkout + ユーザのSIFで `build_amber_system` を2回実行した。
共通条件はff19SB/OPC、is_membrane=True、hmr=True、pablo_auto_download=False、
box_dimensionsはsolv_005の保存値。変更点は結合リストの有無だけ。

| 条件 | 原子数 | SG–SG結合 | A:490電荷 | A:507電荷 | 全電荷 |
|---|---:|---:|---:|---:|---:|
| 明示計画なし（一時電荷ビルド相当） | 157452 | 8 | −1 | −1 | 約0 |
| prep_008のdisulfide_bonds.jsonをリストとして渡す | 157452 | 9 | 0 | 0 | 約+2 |

490–507のSG間距離は通常topo_005の初期座標で約3.345 Å。
この結合は距離だけの自動検出で作られず、結合計画ありのビルドでは1本が明示追加される。
計画なしでも残基名はCYXに復元されるが、Systemの電荷は各−1。
力場のグラフ照合でCYM相当の化学状態になっており、名前の復元だけでは防げない。

両ビルドともsuccess=Trueかつtopology_validation=passed。
計画なしではdisulfides.status=not_requested、protonation_variantsも21/21 passed。
`amber/topology_validation.py` のCYX検査はHGの不在を確かめるだけで、
SG–SG結合・残基電荷をCYX全件に要求しないことが、見逃しの第二の原因。

solv_005の実メタデータはnet_charge=0、Na/Cl各82個。
本来はタンパク質+2なのでイオン差はCl−Na=2が必要。
ユーザのsolv_006はNaを1個Clに変えた手動修正版であり、その親から作る
通常topo_005も迂回topo_007も全電荷0である。

古いsolv_001の記録はnet_charge=−2。対応する通常topo_001には9本のS–Sがあり、
初期距離が3 Åを超えるものが2本あるため、同じ機序で−4eずれた説明と整合する。
この旧入力での再構築は未実施。solv_007の−1は、酸性別名を持たない別入力の
実際のタンパク質電荷−1と一致しており、+2であるべき同一状態ではない。

## Bug 3とlipid21

現行コードでは、実際のCYX入力とprep_008の9組の結合指定を使った再構築が成功した。
HG付きCYSを直接結合する問題一般や旧SIFの全操作組合せは今回再検証していないが、
「現在の公開引数では利用可能な入力がない」という主張は成立しない。

`_topology_pablo.load_topology` は脂質の認識失敗時にPDBFileへフォールバックする。
topo_005/006の保存レポートと今回の両再構築はすべてused_pablo=False。
`amber/openmm_build.py:1219` 以降に力場テンプレートによる内部結合と外部結合の補完があり、
通常topo_005で28,951本の内部結合、442本の外部結合が追加されている。
Pabloの全廃や新しい脂質接続実装が今回の修正に必須、とは判断できない。

## 修正対象と調査の限界

優先する修正は、中和用ビルドにprep由来の明示結合計画を同じ残基識別で引き継ぐこと。
併せてCYXのSG–SG結合と電荷を検査し、結合計画を解決できない状態を成功にしないこと。
イオン配置後の最終System電荷確認も必要。build_amber_systemにはDAGの
neutralization_expectedに基づく不一致ガードが既にあるため、その契約を維持する。
単にヒューリスティックを部分電荷合計へ置き換える修正では解決しない。

`atom_count_preserved` は現在、出力Topology/System/Stateの相互一致を検査しており、
元入力との保存性を独立に保証する項目ではない。今回は入力と出力を別途数えて補った。

本作業は原因調査であり、製品コードの修正はまだ行っていない。
ユーザの平衡化の温度・密度・全時系列は再解析せず、既存トポロジーの結合/電荷と
対照再構築に焦点を当てた。一時ログと再現スクリプトは `/tmp/smo-bug-audit/` にある。

## 追加調査: 「残基消失」という報告に至る経緯とskill監査

### 実際のCLIにある配列長の不整合

ユーザのSIFと現行コードで公開CLIを実行した。`inspect_molecules` は読み取りのみ、
`split_molecules` の出力先は `/tmp/smo-bug-audit/` とした。

```bash
mdclaw inspect_molecules --structure-file <node>/artifacts/system.topology.pdb
mdclaw split_molecules --structure-file <node>/artifacts/system.topology.pdb \
  --output-dir <temporary-directory> --include-types protein --select-chains A
```

| 入力 | inspect_moleculesのnum_residues / sequence_length | split_molecules.all_chainsのnum_residues / sequence_length | 抽出PDBのCA数 |
|---|---|---|---:|
| topo_005 | 476 / 476 | 476 / **447** | 476 |
| topo_006 | 476 / 476 | 476 / **468** | 476 |
| topo_007（迂回） | 476 / 476 | 476 / 476 | 476 |

単なる仮想の標準20種フィルターに加え、製品の実CLIにも報告の数字を返す場所がある。
原因は検査実装が二重化されていること。

- 公開 `inspect_molecules` は `mdclaw/research/inspection.py:464` の
  `PROTEIN_RESNAMES` を使い、AMBER別名を数える。
- `mdclaw/structure/split.py:364` の `_inspect_molecules_impl` は
  `AMINO_ACIDS` だけを数え、`sequence_length` からHID/CYX/ASH/GLHを除外する。
- この内部実装を `split_molecules`（同ファイル1002行付近）と
  `prepare_complex`（prepare_complex.py:1346）が使用している。
- 同じJSONでも `num_residues` と `chain_file_info.residue_count` は476。
  実際の抽出PDBも476残基なので、このSMO入力における誤りは配列長の表示であり、
  原子削除ではない。

迂回スクリプトはPDBFileでAMBER名をHIS/CYS/ASP/GLUへ正規化するため、
同じ内部検査でも476になる。「迂回後だけ完全に見える」現象を説明できる。
ただし、報告者がこのCLI出力や内部関数を判定に用いたという実行記録は見つからず、
この表示バグが報告の直接原因だった、とはまだ断定できない。
既存prep_008/009のsplit_metadataにある468は、別名を付ける前の元構造の実残基数
（その後8残基を補完）であり、この表示バグの過去の発生記録ではない。

可視化も切り分けた。ユーザのプレビュースクリプトは `polymer.protein` を使う。
同じSIFのPyMOLでtopo_005/006/007をロードすると、いずれも
`polymer.protein and name CA` は476原子。topo_005のCYX SGも18個が選択される。
従って、このプレビューのタンパク質選択自体では29残基欠落を再現しない。

### 保存記録から確定できる時系列（JST）

1. **9/7 15:33**: solv_005が中和電荷0・Na/Cl各82個で完了。
2. **9/7 15:37**: solv_006を作成。nodeの警告にNa→Clの手動修正を明記。
   node_createdイベントはあるがtool_started/tool_completedはない。
3. **9/7 15:39**: 通常のtopo_005が完了。保存XMLに476残基・9本のS–S・電荷0が存在。
4. **9/7 15:58–16:07**: 別枝prep_009→solv_007→topo_006。
   入力からCYX/ASH/GLHを持たず、S–S指定も空。HIDは8個保持されている。
5. **9/8 11:26**: 迂回topo_007を作成。
   node_createdイベントの時刻は11:26:17で、現在のnode.jsonのcreated_atは11:26:32。
   metadataは空、tool_started/tool_completedイベントはなく、warningsに
   「Pabloが29残基を削除」「HIDだけでも8/8消失」と書かれている。
   これは測定結果を持つツールレポートではなく、迂回採用理由として記録された文章。
6. **9/8 11:28–12:43**: 迂回Systemのmin/eqが完了。
   その成功は、前段の通常ビルドが壊れていたことの証拠にはならない。

共有領域のSMO全ファイル、およびユーザ領域のスクリプト・テキスト・JSON・隠しファイルを
対象に、残基消失の文言と検査コードを検索した。保存された最も直接的な主張は
上記topo_007/node.jsonのwarningsであり、独立した最小再現コード・失敗出力は未発見。
`direct_openmm_test/` には迂回構築スクリプト4本があるが、通常ビルドに対して
29個の欠落を測定した検査コードはない。

### skillの評価

ユーザcheckoutの全55個のMarkdown skillを比較した。
差はhpc-run/SKILL.md、hpc-run/sif-slurm.md、common/preamble.mdのみ。
md-prepare、md-analyze、md-equilibration、visual-qa、tool-outputは現行版と同じ。
ただし、当該会話が実際にどのインストール先のskillを読んだかは会話ログが必要。

**明示的に誤った残基削除を指示するskillは見つからない。**
むしろprep-chemistry.mdはHID/CYX/ASH/GLHを対応済みと説明し、
S–Sを下流処理の失敗回避のために消すことを禁止している。
visual-qa.mdも画像から化学的正しさを判定しないよう明記している。

一方で、誤診を防止する案内には以下の不足がある。

- `md-prepare/explicit-water.md:84` 付近はビルドをPablo経路として説明するが、
  PDBFileフォールバックと脂質結合補完には触れていない。
  実際に使用した経路を `topology_validation.loader.used_pablo` で確認する指示もない。
  この省略はPabloへの誤帰属を誘いやすいが、今回の報告への因果関係は未確認。
- topoのHandoffはcompleted確認を案内するが、残基消失などの異常を疑った場合に、
  入出力の原子・残基対応とSystem内結合を比較する専用手順がない。
  配列長、残基数、選択結果、残基名の正規化を区別する案内もない。
- `md-analyze` の既定selection="protein"は解析用の部分集合であり、
  完全性監査の全残基一覧と同義ではない、という説明がない。
  MDTrajは今回475、PyMOLは476、内部配列長は447と異なるため、選択系の明示が必要。
- common/run-loop.mdはファイル移動や終端ノードの書き換えを避けるよう指示する。
  今回の手動修正/迂回ノードには通常の実行イベントがなく、根拠となる検査の
  コマンド・対象ファイル・観測結果も付いていない。skillが指示した標準経路ではない。
  ただし診断実験をユーザが別途認めた可能性があり、無断操作だったとは判断しない。

以上は「skillが誤診を直接起こした証拠」ではなく、独立に確認した説明・診断手順の不足。
CLI側の検査実装の統一に加え、skillには異常時の共通検証手順を設け、
実行経路、元入力との対応、Systemの結合/電荷、実測と推測を残すことが適切。

### 未確定の最後の接点

`/home/rku00140` は所有者のみアクセス可能で、会話履歴・シェル履歴は
OSのアクセス権により読めない。権限の回避は行っていない。
共有領域には当該会話ログは見つからなかった。

従って現時点で確定できるのは、①報告の数字を返す実CLIバグ、
②迂回でその数字が正常化する機序、③誤った消失主張が保存された時点、
④skillの誤診防止上の不足まで。
**誰/どのエージェントが、どの検査値を見てPabloによる原子削除と断定したか**は未確定。
その確定には、9/8 11:26以前のSMO会話ログ、検査コマンドとstdout、参照ファイル名が必要。
共有可能な会話ログのパスをユーザに問い合わせ済み。

## 最終再調査: 旧S–S失敗の原因確定と、証拠不足項目の終了

ユーザの指示に従って探索範囲を拡大し、情報が残っていない項目はここで調査を閉じる。
本節は前節の「旧版S–S失敗原因は未確定」という評価を更新する。

### 探索範囲

- 現プロジェクト `/data1/rkp00079/rku00140` と、追加発見した旧プロジェクト
  `/data1/rkp00048/rku00140`。git内部・膜キャッシュを除くファイル一覧は各6079/5114件。
  これは棚卸し数であり、全ファイルの内容を手作業で読んだという意味ではない。
- 両領域の関連JSON/JSONL、スクリプト、Markdown、ログ、Slurm出力、隠しファイルを
  SMO/6XBL/残基消失/ヒスチジン/S–S失敗の語とパスで検索。
- 6XBLジョブの全failure/latest/tool_result.json、全topoのamber_metadata、
  node.jsonとappend-onlyイベントを照合。
- 旧領域のSMO、CHARMM-GUI試行、ジョブインデックスを調査。8/21の別SMO試行には
  別のプロトン化検証失敗があったが、6XBLの29残基消失を証明するものではなかった。
- skillの変更履歴と `8e703ed` 前後のコードを照合。
- `/tmp` はSMO/ユーザ名などの関連ファイル名の棚卸しに限定。
  他ユーザの無関係な一時ファイル内容は調査していない。
- `/fast1/rkp00079/rku00140` と `/fast1/rkp00048/rku00140` は存在しない。
  `/home/rku00140` は引き続き所有者限定で読めない。権限回避は行っていない。

### Bug 3: 9/3の実失敗の機序を再現した

`topo_004/artifacts/failure/latest/tool_result.json` に保存されたエラーは、
CYSの原子組成に対して外部S結合が1個多いというテンプレート不一致。
同じディレクトリの `amber_metadata.json` の `disulfide_bond_plan` は9組すべてが
`skipped_cys_protonated` だった。

親の `prep_005` はS–Sを持たない枝であり、`disulfide_pairs=[]`。
そのmerged.pdb、solv_004のmembrane.pdb、topo_004のsystem.prepared.pdbには
CYS64/178を含む対象CYSのHGが実際に存在する。
しかしtopo_004のビルドには9組の明示結合指定が入っていたことが上記planから分かる。

現行コードとユーザSIFで、保存された `topo_004/artifacts/system.prepared.pdb` を
そのまま使用して対照実験を行った。box/ff19SB/OPC条件は保存メタデータと同じ。
出力は `/tmp/smo-bug-audit/final-sweep/`。

| 条件 | 結果 |
|---|---|
| HG付きCYS、保存メタデータの9組を指定 | 9/3と同じ `No template found for residue 6 (CYS)`、外部S結合過剰で失敗 |
| 同じPDB、結合指定を空にする | 成功、157532原子、全電荷約0 |

この比較は還元状態と酸化状態の物理的な等価性を示すものではない。
「HG付きのまま追加したS–S結合」が失敗を作ることを切り分けるための診断実験。

構造ロード直後のSG–SG結合は0本。計画関数は9組すべてをスキップと報告する。
それにもかかわらず `add_disulfide_bonds` に元の指定を渡すと9本が追加され、HGは残る。
`build_amber_system` 内の実際の呼び出しもこの流れになっている。

1. `amber/build_system.py:1336` で `_plan_disulfide_topology_bonds` を呼ぶ。
2. `amber/topology_bonds.py:260` 付近でCYSを `skipped_cys_protonated` とする。
3. **計画のスキップ結果を実行制御に使わず**、build_system.py:1410で元の
   `disulfide_bonds` をビルド実装に渡す。
4. `amber/openmm_build.py:1113` 付近から `_topology_pablo.add_disulfide_bonds` を呼び、
   同関数はHGの有無を確認せず `topology.addBond` する。
5. CYS–HGとSG–SGが同居してテンプレート不一致になる。

つまり「水素があるのでAPIが早期に拒否する」というより、**スキップと記録した結合を
実際には追加してしまう計画・実行の不整合**が、保存された旧失敗の直接原因。
現在も同じ機序で再現する。

`8e703ed` の変更対象はclean_proteinとテスト・memoであり、上記のAmberビルド経路は
変更されていない。直前コミットにも同じ計画呼び出しと未加工引数の引き渡しがある。
したがってこの不整合を `8e703ed` が修正した、とは言えない。

ただし**「有効な入力状態が存在しない」という主張は別であり、成立しない**。
9/2の通常topo_003はcompletedイベントと9本のSystem S–S結合を持ち、
現行コードでもHGを持たないCYX + 正しい9組の計画は成功する。
修正は要求の化学的整合性を事前に検証し、計画と実行を一致させること。
prepが所有する水素再構成を無断でtopoへ移すこととは区別する。

### 「completed」だけでは過去の実行を復元できない実例

topo_001の現在のnode.jsonはcompletedだが、9/2の実行イベントは
`tool_failed` / `neutralization_charge_mismatch`、amber_metadataもsuccess=False。
保存Systemはタンパク質+2とイオン+2で全電荷+4であり、実際には電荷ガードが働いている。
その後の通常のtool_completedイベントはない。

このため最初の棚卸しで見た現在のnode statusを、9/2のビルドが正常完了した証拠には
使わない。topo_005/006の保存Systemを独立検査した結論には影響しない。
skillのcompleted確認は通常の実行管理には有効だが、手動介入後のバグ調査では
イベント・ツール結果・実ファイルを照合する必要がある。

### 最終判定

| 項目 | 判定 |
|---|---|
| 中和電荷誤り | 原因確定。中和用ビルドへのS–S計画欠落とCYX検証不足 |
| 残基消失と同じ数字を返すCLI | 原因確定。二重化された検査実装のAMBER別名除外。実残基は消えない |
| 9/3のHG付きCYS + S–S指定失敗 | 原因確定。計画はスキップ、実行は結合追加という不整合。現行でも再現 |
| S–Sの有効入力が一切ない | 保存された旧成功例と現行再現で否定 |
| lipid21のPDBFile接続経路がない | 実装・保存成果物・再構築で否定 |
| 古い科学計算コードを使用した可能性 | 確認できたSIF/checkoutでは該当せず。科学計算コードは現行と同じ |
| 実際にどの検査値から消失と断定したか | 元の検査・会話ログがないため確定不能。証拠不足として終了 |
| 実際に読んだskillと誤診への直接因果 | 会話ログがないため確定不能。説明不足の指摘までで終了 |
| lipid21経路がないと判断した経緯 | 診断文章以外の根拠が残っておらず、証拠不足として終了 |

新たな会話ログが提供されない限り、同じ成果物を繰り返し調べても最後の帰属は決まらない。
ユーザの指示に従い、この証拠不足部分はこれ以上追わない。
製品コード・skill・ユーザのDAGは未変更。調査用のビルドは終了している。
