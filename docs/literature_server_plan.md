# PubMed Literature Server Implementation Plan

## Overview

MDZenのPhase 1（Clarification）で文献検索機能を追加し、曖昧なクエリに対してまず関連論文を検索してからユーザーに確認する「文献ファースト」ワークフローを実現する。

## A. `servers/literature_server.py` の実装

### 1. サーバー構成

```python
from fastmcp import FastMCP
import httpx
import xml.etree.ElementTree as ET
import os
import asyncio
import logging

mcp = FastMCP("Literature Server")
logger = logging.getLogger(__name__)

# NCBI E-utilities base URLs
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
```

### 2. ツール定義

#### `pubmed_search(query, retmax=10, sort="relevance")`

**目的**: PubMed検索クエリを実行し、PMID一覧と基本情報を返す

**パラメータ**:
| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| query | str | (必須) | PubMed検索クエリ（Boolean演算子対応） |
| retmax | int | 10 | 最大取得件数 (1-100) |
| sort | str | "relevance" | ソート順: "relevance", "date", "first_author" |

**実装詳細**:
```python
@mcp.tool()
async def pubmed_search(
    query: str,
    retmax: int = 10,
    sort: str = "relevance"
) -> dict:
    """Search PubMed for scientific literature.

    Args:
        query: PubMed search query. Supports Boolean operators (AND, OR, NOT)
               and field tags like [Title], [Author], [MeSH Terms].
               Example: "adenylate kinase AND molecular dynamics[Title]"
        retmax: Maximum number of results to return (1-100, default: 10)
        sort: Sort order - "relevance" (best match), "date" (most recent),
              or "first_author" (alphabetical by author)

    Returns:
        dict with:
          - success: bool
          - count: total hits
          - pmids: list of PMIDs
          - articles: list of {pmid, title, authors, journal, year, doi}
          - errors: list of error messages
    """
```

**API呼び出しフロー**:
1. `esearch.fcgi` でPMID一覧を取得
2. `esummary.fcgi` で基本メタデータを取得（title, authors, journal, year）
3. 結果を構造化して返す

#### `pubmed_fetch(pmids, include_abstract=True)`

**目的**: PMID一覧からアブストラクト含む詳細情報を取得

**パラメータ**:
| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| pmids | str または list | (必須) | カンマ区切りPMID文字列またはリスト |
| include_abstract | bool | True | アブストラクト取得有無 |

**実装詳細**:
```python
@mcp.tool()
async def pubmed_fetch(
    pmids: str | list[str],
    include_abstract: bool = True
) -> dict:
    """Fetch detailed information for specific PubMed articles.

    Args:
        pmids: PubMed IDs as comma-separated string or list.
               Example: "12345678,23456789" or ["12345678", "23456789"]
        include_abstract: Whether to include abstracts (default: True)

    Returns:
        dict with:
          - success: bool
          - articles: list of detailed article info including abstracts
          - errors: list of error messages
    """
```

**API呼び出し**: `efetch.fcgi` (rettype=xml, retmode=xml) でPubMed XMLを取得

### 3. 環境変数

| 変数名 | 説明 | デフォルト |
|--------|------|-----------|
| MDZEN_NCBI_API_KEY | NCBI APIキー（レート制限緩和） | None |
| MDZEN_NCBI_EMAIL | NCBI推奨のメールアドレス | None |

### 4. レート制限対策

```python
async def _ncbi_request(client: httpx.AsyncClient, url: str, params: dict, max_retries: int = 3) -> str:
    """Execute NCBI request with exponential backoff retry."""
    api_key = os.environ.get("MDZEN_NCBI_API_KEY")
    email = os.environ.get("MDZEN_NCBI_EMAIL")

    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email

    for attempt in range(max_retries):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:  # Rate limited
                wait_time = 2 ** attempt  # 1, 2, 4 seconds
                logger.warning(f"Rate limited, waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded")
```

### 5. XMLパース

```python
def _parse_esummary(xml_text: str) -> list[dict]:
    """Parse esummary XML response."""
    root = ET.fromstring(xml_text)
    articles = []
    for doc in root.findall(".//DocSum"):
        pmid = doc.find("Id").text
        items = {item.get("Name"): item.text for item in doc.findall("Item")}
        articles.append({
            "pmid": pmid,
            "title": items.get("Title", ""),
            "authors": items.get("AuthorList", "").split(", ")[:3],  # First 3 authors
            "journal": items.get("Source", ""),
            "year": items.get("PubDate", "")[:4],
            "doi": items.get("DOI", ""),
        })
    return articles

def _parse_efetch(xml_text: str) -> list[dict]:
    """Parse efetch XML response with abstracts."""
    root = ET.fromstring(xml_text)
    articles = []
    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        pmid = medline.find("PMID").text
        article_elem = medline.find("Article")

        # Extract abstract
        abstract_elem = article_elem.find(".//Abstract/AbstractText")
        abstract = abstract_elem.text if abstract_elem is not None else ""

        # Handle structured abstracts
        abstract_parts = article_elem.findall(".//Abstract/AbstractText")
        if len(abstract_parts) > 1:
            abstract = " ".join([
                f"{p.get('Label', '')}: {p.text or ''}"
                for p in abstract_parts
            ])

        articles.append({
            "pmid": pmid,
            "abstract": abstract,
            # ... other fields
        })
    return articles
```

### 6. エントリポイント

```python
def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="Literature Server (PubMed)")
    parser.add_argument("--http", action="store_true", help="Use HTTP transport")
    parser.add_argument("--sse", action="store_true", help="Use SSE transport")
    parser.add_argument("--port", type=int, default=8008, help="Port for HTTP/SSE")
    return parser.parse_args()

if __name__ == "__main__":
    args = _parse_args()
    if args.http:
        mcp.run(transport="http", host="0.0.0.0", port=args.port)
    elif args.sse:
        mcp.run(transport="sse", host="0.0.0.0", port=args.port)
    else:
        mcp.run()
```

---

## B. `src/mdzen/tools/mcp_setup.py` の更新

### 1. ポートマッピング追加

```python
SSE_PORT_MAP = {
    "research": 8001,
    "structure": 8002,
    "genesis": 8003,
    "solvation": 8004,
    "amber": 8005,
    "md_simulation": 8006,
    "metal": 8007,
    "literature": 8008,  # 新規追加
}

HTTP_PORT_MAP = SSE_PORT_MAP.copy()
```

### 2. `create_mcp_toolsets()` 更新

```python
def create_mcp_toolsets() -> dict[str, McpToolset]:
    """Create MCP toolsets for all servers."""
    server_names = [
        "research", "structure", "genesis", "solvation",
        "amber", "md_simulation", "literature"  # 追加
    ]
    # ... rest of implementation
```

### 3. `get_clarification_tools()` 更新

**重要**: literature を research より前に配置

```python
def get_clarification_tools() -> list[McpToolset]:
    """Get tools for clarification phase (Phase 1).

    Order matters: literature search should be available before
    structure database queries for ambiguous requests.
    """
    return [
        # Literature search first
        create_filtered_toolset(
            "literature",
            tool_filter=["pubmed_search", "pubmed_fetch"],
        ),
        # Then structure databases
        create_filtered_toolset(
            "research",
            tool_filter=[
                "search_structures",
                "get_structure_info",
                "inspect_molecules",
                "download_structure",
                "get_alphafold_structure",
            ],
        ),
    ]
```

---

## C. `src/mdzen/prompts/clarification.md` の更新

### 1. Literature Tools セクション追加

```markdown
## Available Tools

### Literature Tools (Use First for Ambiguous Queries)
- `pubmed_search(query, retmax, sort)`: Search PubMed for relevant papers
- `pubmed_fetch(pmids, include_abstract)`: Get detailed article information

### Structure Database Tools
- `search_structures(query)`: Search RCSB PDB
- `get_structure_info(pdb_id)`: Get PDB entry details
- ...
```

### 2. ワークフロー指示の更新

```markdown
## Workflow Guidelines

1. **For Ambiguous Queries** (e.g., "simulate adenylate kinase dynamics"):
   - FIRST: Search PubMed for recent MD studies on the target
   - Extract: PDB IDs, simulation parameters, force field choices from literature
   - Ask user: "Found these relevant studies: [list]. Which approach interests you?"
   - THEN: Proceed with structure retrieval

2. **For Specific Queries** (e.g., "simulate PDB 1AKE at 300K"):
   - Skip literature search if PDB ID is explicitly provided
   - Proceed directly to structure inspection and parameter clarification

3. **DON'T**:
   - Jump directly to `download_structure` for vague protein names
   - Skip asking about simulation goals for complex systems
```

### 3. 具体例の追加

```markdown
## Examples

### Example: Ambiguous Query
User: "I want to simulate adenylate kinase"

Good Response:
1. `pubmed_search("adenylate kinase molecular dynamics simulation", retmax=5)`
2. "Found 3 recent studies:
   - Smith et al. (2023) used PDB 4AKE with AMBER ff19SB, 500ns NPT
   - Lee et al. (2022) compared open/closed conformations using 1AKE/4AKE
   - ...
   Which conformation are you interested in? Do you have a specific PDB in mind?"

Bad Response:
1. `download_structure("1AKE")` (assuming without asking)
```

---

## D. その他の更新

### 1. `main.py` - list-servers コマンド

```python
# servers_info に追加
SERVERS_INFO = {
    # ... existing servers ...
    "literature": {
        "file": "literature_server.py",
        "description": "PubMed literature search",
        "tools": ["pubmed_search", "pubmed_fetch"],
    },
}
```

### 2. `CLAUDE.md` 更新

```markdown
The system uses **7 independent FastMCP servers**:

1. **research_server.py** - External database integration
2. **structure_server.py** - Structure preparation
3. **genesis_server.py** - AI structure prediction
4. **solvation_server.py** - Solvation and membrane setup
5. **amber_server.py** - Topology generation
6. **md_simulation_server.py** - MD execution
7. **literature_server.py** - PubMed literature search  # NEW
```

### 3. テスト追加 (`tests/test_literature_server.py`)

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_pubmed_search_basic():
    """Test basic PubMed search functionality."""
    # Mock NCBI response
    mock_esearch_response = """
    <eSearchResult>
        <Count>100</Count>
        <IdList>
            <Id>12345678</Id>
            <Id>23456789</Id>
        </IdList>
    </eSearchResult>
    """
    # ... test implementation

@pytest.mark.asyncio
async def test_pubmed_search_rate_limit_retry():
    """Test exponential backoff on rate limiting."""
    # ... test 429 response handling

@pytest.mark.asyncio
async def test_pubmed_fetch_with_abstract():
    """Test fetching article details with abstracts."""
    # ... test implementation
```

---

## 実装順序

1. **Phase 1**: `servers/literature_server.py` の基本実装
   - pubmed_search (esearch + esummary)
   - pubmed_fetch (efetch)
   - レート制限対策

2. **Phase 2**: MCP統合
   - `mcp_setup.py` のポート追加
   - `get_clarification_tools()` の更新

3. **Phase 3**: プロンプト更新
   - `clarification.md` の更新
   - ワークフロー指示の追加

4. **Phase 4**: テスト・ドキュメント
   - ユニットテスト追加
   - `CLAUDE.md` 更新
   - `main.py` list-servers 更新

---

## 依存関係

- **追加依存**: なし（httpx, xml.etree は既存）
- **オプション**: `MDZEN_NCBI_API_KEY` でレート制限緩和（10 req/s → 無制限）

## リスク・考慮事項

1. **NCBI API制限**: APIキーなしは3 req/s、指数バックオフで対応
2. **XML構造変更**: NCBIのXMLスキーマ変更に備えて堅牢なパース
3. **タイムアウト**: 大量PMIDのfetchは時間がかかる可能性
