# リガンドの絶対結合自由エネルギー（ABFE）— 設計判断と検証手順

`mdclaw/fep/abfe.py`・`decouple.py`・`boresch.py`（2026-09-20）。1 点変異 ddG の
hybrid-topology FEP（`fep-references.md`）の DAG・λ 窓サンプリング・MBAR をそのまま使い、
**新規は「デカップリング用 System」「Boresch 拘束」「標準状態補正つきの最終ノード」の 3 つだけ**。

## 1. 熱力学サイクル

```
dG_bind° = dG(solvent leg) − dG(complex leg) + dG_restraint − kT ln σ
```

- 各 leg の dG は「λ=0: 完全結合・拘束なし → λ=1: デカップル（complex は拘束あり）」の MBAR 値。
  complex leg の値は拘束を入れるコストを含む。
- `dG_restraint`（正）= 相互作用のないリガンドを 1 M 標準状態から拘束下に置く自由エネルギー。
  Boresch 2003 eq. 32:
  `kT ln[ 8π² V° √(K_r K_θA K_θB K_φA K_φB K_φC) / (r0² sin θA0 sin θB0 (2π kT)³) ]`、
  `V° = 1.6605 nm³`。既定の力の定数（K_r = 4184 kJ/mol/nm²、K_角 = 83.68 kJ/mol/rad²）、
  r0 = 0.5 nm で約 +33 kJ/mol（+7.9 kcal/mol）。`tests/test_abfe.py` が配置積分の数値積分と
  0.15 kJ/mol 以内で一致することを固定している（符号も含めて）。
- `−kT ln σ`: 拘束がリガンドを σ 個の区別できない向きの 1 つに閉じ込めるときの補正
  （Mobley 2007。ベンゼンは σ = 12 で −1.5 kcal/mol）。既定 1、`--ligand-symmetry-number`。

## 2. DAG

```
source ─ prep ─ solv ─ topo[build_decoupled_system] ─ min ─ eq ─ topo[add_boresch_restraint] ─ fep… ─ analyze_fep ─┐
           └─ prep[extract_ligand] ─ solv ─ topo[build_decoupled_system] ─ min ─ eq ─ fep… ─ analyze_fep ──────────┴─ analyze[estimate_binding_dg]
```

| 判断 | 採った案 | 理由 |
|---|---|---|
| leg の置き場 | 1 job に 2 leg、solvent leg は complex の prep の子 prep（`extract_ligand`） | ddG と同じ形。両 leg が同じ `ligand_chemistry` 記録（= 同じパラメータ化入力）を読む。記録内のパスは DAG が読み出し時に絶対化、書き込み時に相対化（`../prep_001/artifacts/…`）するのでコピー不要 |
| Boresch 拘束の置き場 | complex leg の **eq の下の topo ノード**（`add_boresch_restraint`）。`_ALLOWED_PARENT_TYPES["topo"]` に `eq` を追加（auto-parent の対象外） | 拘束の 6 原子と基準値は leg の全窓で同一でなければならず、`run_fep` は窓を複数 fep ノードに分けるので、定義を所有するノードが 1 つ要る。平衡化後の姿勢でしか決められないので eq の後。run 側で System を足し引きする箇所を増やさない（「run 側は System を再構成しない」契約）。fep ノードはこの topo の直下に置く（`_ALLOWED_PARENT_TYPES["fep"]` に `topo` を追加）。窓の開始状態は祖先をさかのぼって最初の eq の state（`_resolve_md_restart`）で、各窓が自分の λ で最小化と平衡化をしてから採るので、拘束つきの再平衡化ノードは物理的に不要 — 最初の実装は「fep の親は eq / fep のみ」という型の都合で eq を 1 つ挟んでいたが、ユーザーの指摘で外した。代わりに、eq の祖先を持たない fep（例: hybrid topo の直下）は入力解決で `fep_equilibration_required` として拒否する |
| PLUMED を使わない | OpenMM の `CustomCompoundBondForce` + global parameter `fep_restraint` | 拘束の強さを λ でスケールし、各サンプルを全窓の λ で再評価する（MBAR の u_kn）必要がある。global parameter なら既存の `run_fep` のループでそのまま動く。PLUMED のバイアスは OpenMM の parameter で制御できず、窓ごとの入力と COLVAR からのオフライン再評価が要る |
| 拘束なしの complex に fep を走らせない | `build_decoupled_system` は complex leg では **`fep_protocol` を書かない**（`metadata.fep.restraint_required`）。その下の fep は入力解決で `abfe_restraint_required` | デカップルしたリガンドは箱中をさまよい、数値は出るが無意味。スキルの注意書きではなくツールで止める |
| leg の判定 | 中身から自動（リガンド・水・単原子イオン以外があれば complex） | エージェントの判断点を作らない |
| 電荷の扱い | 静電は **annihilate**（電荷を 0 へ。分子内クーロンも消える）、立体は **decouple**（リガンド内 LJ は別の `CustomNonbondedForce` で常時フル） | openmmtools / YANK の既定。PME 下で静電を decouple するには分子内の全ペアを別の力で足し戻す必要がある。分子内クーロンの消失分は両 leg で同一なので結合自由エネルギーでは相殺。**単独の solvent leg は水和自由エネルギーではない**（真空 leg が要る） |
| λ の契約 | hybrid の 5 parameter のうち `fep_elec_old` / `fep_sterics_old` を流用（残りは宣言して 0 固定）、complex は 6 番目 `fep_restraint`。protocol が `global_parameters` と `phases` を自分で名乗る | `run_fep` / `analyze_fep` を ABFE 用に分岐させない。窓は明示的な parameter 値を持つので順序（拘束 on → 電荷 off → 立体 off）は protocol ファイルが契約 |
| 既定の窓 | 拘束 `0,0.02,0.04,0.08,0.2,0.5,1`（0.04 は回転するリガンドが 1 つの井戸に絞られる区間。実効角度幅 70 → 50 → 35°）、電荷 `1,0.75,0.5,0.25,0`、立体 14 点（0 付近を密に）→ complex 24 窓、solvent 18 窓 | 電荷は立体が完全に on のうちに消す（裸の電荷がソフトコア内に入らない） |

## 3. Boresch 原子の選択（ツール側で完結）

1. eq の state から結合状態（拘束なし）で 200 ps の NVT を走らせ、2 ps ごとにフレームを取る。
2. リガンド側: 結合した重原子 3 つの鎖 (A, B, C)。A は重原子重心に近い順に最大 4 候補、B は A の隣接で次数最大、C は B（なければ A）の隣接。結合は **System の結合項と拘束から取る**（`topology.pdb` は CONECT を持たず、非標準残基は読み戻すと無結合になる — 3PWB の実走で判明）。
3. 受容体側: 各残基の主鎖 (c, b, a) = (N, C, CA)。a–A の初期距離が 0.4–1.5 nm のものだけ。
4. 6 座標の時系列（最小像、二面角は円周統計）から、θA・θB の基準値が [40°, 140°] に入る候補だけ残し、Σ(std / 熱的幅)² が最小の組を採る。基準値は距離が平均、角度は**最も多い向きのフレームの値**（5 つの周期座標の同時カーネル密度が最大のフレーム。帯域幅は熱的幅。座標ごとの最頻値を組み合わせると実在しない向きになりうるので 1 フレームから取る。Mobley 2006 のヒストグラム最頻値と同じ考え）。
5. 最良候補でも std(r) > 0.15 nm なら `abfe_restraint_unstable` で拒否（リガンドが部位を離れている）。角度 std > 25° は拒否しない: 部位に留まったまま向きを変えるリガンド（T4L 空洞のベンゼン: r std 0.03 nm、面内回転で二面角 1 つの std 86°、200 ps で 6 つの等価な向きを 103 回乗り換え）は最も多い向きに拘束し、`statistics.reorients` と該当座標を記録して警告を出す。閉じ込めのコストは拘束相の FEP が払うので、`estimate_binding_dg` はこの leg で σ > 1 を `abfe_symmetry_already_sampled` として拒否する（サンプリングされた向きの二重計上を防ぐ）。選択実行で訪れなかった等価な向き（平面環の裏返しなど）は補正しない近似で、1 つの 2 回対称につき最大 kT ln 2 = 0.42 kcal/mol。9/26 までの実装は角度 std > 25° も拒否しており、ベンゼンで「平衡化を延ばせ」と誤った案内を出していた。

内部座標の符号規約は OpenMM の `distance/angle/dihedral` と一致させてある。最初の実装は二面角の符号が逆で、
「測った基準値で力のエネルギーが 0 になる」テストがそれを捕まえた（`test_coordinates_follow_openmm_conventions`）。

## 4. 端点検証（`validate_decoupling`）

- 結合状態: alchemical System（dispersion 補正 off）= 元の System。
- デカップル状態: リガンド全体を別の原子の上に平行移動してもエネルギー不変（環境と相互作用していない証明。
  接触してもソフトコアが有限であることも同時に確認）。

実測: 溶媒和 ACE-ALA-NME で両方 1e-4 kJ/mol、3PWB/GOL 複合体（CUDA 単精度、|E| ≈ 5.7e5 kJ/mol）で
0.47 / 0.007 kJ/mol（許容 1.14）。

## 5. v1 の範囲外（拒否コードで止める）

- 荷電リガンド（`abfe_charged_ligand_unsupported`）。箱電荷が変わり、有限サイズ誤差が 2 leg で相殺しない。
  co-alchemical ion（`fep/coion.py`）を流用すれば対応可能だが、まず中性で検証する。prep 記録の電荷でビルド前に、
  割り当て後の部分電荷の総和でもう一度判定する。
- 共有結合リガンド（`abfe_ligand_covalent`）、重原子 3 つ未満（`abfe_ligand_too_small`）、複数コピー（`abfe_ligand_ambiguous`）。
- リガンドの LJ 長距離補正（hybrid と同じ既知の近似。complex と solvent で周囲の密度が違うので完全には相殺しない。
  YANK は端点の再重み付けで補正する）。
- 膜タンパク質、核酸受容体（受容体アンカーは蛋白質主鎖 N/CA/C 前提 → `abfe_receptor_missing`）。
- 複数の結合姿勢、蛋白質側の遅い構造変化（T4L L99A の Val111 など）。数値は「調製した姿勢」の値。

## 6. 検証状況

| 段階 | 状況 |
|---|---|
| 単体（protocol、リガンド選択、解析補正 vs 数値積分、OpenMM 規約、原子選択と拒否、DAG ノード、サイクルの足し算） | `tests/test_abfe.py`（fast 10 本 + slow 1 本）pass |
| 配線 e2e | 3PWB（トリプシン）+ GOL を既存の `fetch_structure → prepare_complex → solvate_structure` に載せ、両 leg を `estimate_binding_dg` まで完走（1 窓 4 ps。**数値に意味はない**）。BEN（+1）は拒否、`--ligand` 無指定は候補付きで拒否 |
| 物理の検証 | **未実施**。下の §7 を別テスターが実施する |

## 7. 物理検証の手順書: T4 リゾチーム L99A / ベンゼン

ABFE の定番ベンチマーク。実験値 **−5.19 kcal/mol**（Morton et al. 1995）。計算値は力場と手順で −4 〜 −6 kcal/mol
に散らばる（Mobley 2007 ほか）。中性・剛直・重原子 6 個で、v1 の範囲にちょうど収まる。

1. 構造: PDB **181L**（T4L L99A + ベンゼン、リガンド残基名 `BNZ`）。`skills/md-abfe/SKILL.md` に従う。
   `prepare_complex --include-types protein ligand --process-ligands --ligand-smiles '{"BNZ": "c1ccccc1"}'`。
   結晶の他のヘテロ原子（BME、Cl など）は保持しない。
2. 力場 ff19SB + OPC（または ff14SB + TIP3P。どちらかに固定して両 leg 同じ）、HMR 既定、NPT 300 K。
3. complex leg: `build_decoupled_system --ligand BNZ` → min → eq（NPT 1 ns 以上）→ `add_boresch_restraint`
   （eq の下の topo）→ その topo の下に fep ノード、`run_fep`（23 窓 × 5 ns を目安、ジョブアレイは
   `skills/md-fep/windows.md`）→ `analyze_fep`。
4. solvent leg: `extract_ligand --ligand BNZ` → `solvate_structure --dist 12` → `build_decoupled_system` → min → eq
   → `run_fep`（18 窓 × 2 ns）→ `analyze_fep`。
5. `estimate_binding_dg --ligand-symmetry-number 1`（ベンゼンは選択実行で回転するので σ は付けない）。

報告してほしいもの:

- `dG_bind_kcal_mol ± error` と `terms_kj_mol` の 4 項、各 leg の `phases`、`min_neighbour_overlap`、窓あたりのサンプリング時間。
- `add_boresch_restraint` の `boresch` ブロック（選ばれた 6 原子の名前、r0・角度、`statistics.std`）。
  **ベンゼンは空洞内で面内回転する**ので `reorients` の警告が出る。その場合 σ は 1（`--ligand-symmetry-number 12` は拒否される）。
- 収束: 窓あたり時間を半分にしたときの dG_bind の変化、`restrain` 相の overlap。
- 拒否や案内（`code` / `next_action` / `next`）で次の一手が決まらなかった箇所。

判定の目安: 実験値から ±1.5 kcal/mol 以内なら実装は妥当。系統的に数 kcal ずれる場合に疑う順は
(1) 符号・項の足し方（`terms_kj_mol` で手計算）、(2) 拘束が強すぎて結合状態を歪めている（`restrain` 相の dG が
数 kcal を超える）、(3) LJ 長距離補正の欠落（§5）、(4) Val111 の回転異性体のサンプリング不足。

## 参考文献（Crossref で書誌確認済み、`citation-audit-2026-09-06.md` 第 3 addendum）

- Boresch, Tettinger, Leitgeb, Karplus, J. Phys. Chem. B 107, 9535 (2003) — 6 座標の拘束と解析的補正。
- Gilson, Given, Bush, McCammon, Biophys. J. 72, 1047 (1997) — double decoupling と標準状態。
- Mobley, Graves, Chodera, McReynolds, Shoichet, Dill, J. Mol. Biol. 371, 1118 (2007) — T4L モデル部位、対称性補正。
- Beutler et al., Chem. Phys. Lett. 222, 529 (1994) — ソフトコア（hybrid と共通）。
