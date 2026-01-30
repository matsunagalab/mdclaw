# Auto-answer testing (PTY不要)

MDZen workflow v2 は `workflow_state.json` の `awaiting_user_input/pending_questions` を使ってユーザー確認を挟みます。
このドキュメントは、**Cursor / Claude Code 等のコーディングエージェントを外部ドライバとして**回答を注入し、PTY無しでもE2Eテストできるようにするためのプロトコルです。

## 1) 対話実行 + 外部注入（推奨：E2E自動化）

起動（TTYが無い環境でもOK）:

```bash
MDZEN_AUTO_ANSWER=true python main.py run "Setup MD for PDB ID 1AKE"
```

または:

```bash
python main.py run --auto-answer "Setup MD for PDB ID 1AKE"
```

`awaiting_user_input=True` になると、jobディレクトリに `questions.json` が書かれます。

- `job_xxxxxxxx/questions.json`: 外部ドライバが読む（ポーリング向け）
- `job_xxxxxxxx/answers.json`: 外部ドライバが書く（回答注入）

### `questions.json`（MDZen → ドライバ）

最小スキーマ（v1）:
- `schema_version`: `1`
- `session_id`: `job_xxx`
- `current_step`: 例 `select_prepare`
- `awaiting_user_input`: `true`
- `pending_questions`: 質問配列
- `suggested_reply_format`: 例 `A no`
- `detected.protein_chains` / `detected.ligands`

### `answers.json`（ドライバ → MDZen）

例:

```json
{
  "schema_version": 1,
  "text": "A no",
  "source": "cursor-agent",
  "step": "select_prepare",
  "created_at": "2026-01-29T12:34:56Z"
}
```

MDZenは `answers.json` を検出すると読み取り、**原子的に消費**します（`answers.consumed.<ts>.<pid>.json` にリネーム）。

### タイムアウト/ポーリング

- `MDZEN_ANSWER_TIMEOUT_S`（default: `300`）: 外部注入待ち上限
- `MDZEN_ANSWER_POLL_INTERVAL_S`（default: `1.0`）: ポーリング間隔

## 2) バッチ（`-p`）: 推奨/既定を自動採用して止まらない

```bash
python main.py run -p "Setup MD for PDB ID 1AKE"
```

`awaiting_user_input=True` になった場合、`pending_questions` から `default: ...` を抽出して自動回答します。
（既定回答が作れないケースのみ停止します）

## 3) テスト用 LLM 自動回答（任意）

外部注入が無い場合に、`pending_questions` へLLMで1行回答します（テスト用途のみ想定）。

```bash
MDZEN_AUTO_ANSWER=true \
MDZEN_TEST_ANSWER_MODEL=openai:gpt-4o-mini \
python main.py run "Setup MD for PDB ID 1AKE"
```

回答モードの優先順位は `MDZEN_AUTO_ANSWER_SEQUENCE` で変更可能です:

```bash
MDZEN_AUTO_ANSWER_SEQUENCE="external,llm,default"
```

## 4) 既定回答の微調整

- `MDZEN_DEFAULT_INCLUDE_LIGANDS`（default: `true`）: ligand質問に対する既定（yes/no）

