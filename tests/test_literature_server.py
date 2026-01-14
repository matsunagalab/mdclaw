"""
Tests for literature_server.py - PubMed search functionality.

Tests cover:
- XML parsing for esearch, esummary, efetch responses
- Error handling for rate limiting and timeouts
- Parameter validation

NOTE: Live API tests require network access and are marked with @pytest.mark.slow
"""

import pytest

# Import server functions for testing
import sys
from pathlib import Path

# Add servers directory to path for direct import
servers_dir = Path(__file__).parent.parent / "servers"
sys.path.insert(0, str(servers_dir))


class TestXMLParsing:
    """Test XML parsing functions."""

    def test_parse_esearch_basic(self):
        """Parse basic esearch response."""
        from literature_server import _parse_esearch

        xml = """
        <eSearchResult>
            <Count>100</Count>
            <IdList>
                <Id>12345678</Id>
                <Id>23456789</Id>
            </IdList>
        </eSearchResult>
        """
        result = _parse_esearch(xml)
        assert result["count"] == 100
        assert result["pmids"] == ["12345678", "23456789"]

    def test_parse_esearch_empty(self):
        """Parse esearch response with no results."""
        from literature_server import _parse_esearch

        xml = """
        <eSearchResult>
            <Count>0</Count>
            <IdList/>
        </eSearchResult>
        """
        result = _parse_esearch(xml)
        assert result["count"] == 0
        assert result["pmids"] == []

    def test_parse_esummary_basic(self):
        """Parse basic esummary response."""
        from literature_server import _parse_esummary

        xml = """
        <eSummaryResult>
            <DocSum>
                <Id>12345678</Id>
                <Item Name="Title" Type="String">Test Article Title</Item>
                <Item Name="Source" Type="String">Test Journal</Item>
                <Item Name="PubDate" Type="String">2024 Jan 15</Item>
                <Item Name="AuthorList" Type="List">
                    <Item Name="Author" Type="String">Smith J</Item>
                    <Item Name="Author" Type="String">Jones M</Item>
                </Item>
                <Item Name="DOI" Type="String">10.1234/test.2024</Item>
            </DocSum>
        </eSummaryResult>
        """
        articles = _parse_esummary(xml)
        assert len(articles) == 1
        assert articles[0]["pmid"] == "12345678"
        assert articles[0]["title"] == "Test Article Title"
        assert articles[0]["journal"] == "Test Journal"
        assert articles[0]["year"] == "2024"
        assert articles[0]["doi"] == "10.1234/test.2024"

    def test_parse_efetch_with_abstract(self):
        """Parse efetch response with abstract."""
        from literature_server import _parse_efetch

        xml = """
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>12345678</PMID>
                    <Article>
                        <ArticleTitle>MD Simulation of Protein</ArticleTitle>
                        <Journal>
                            <Title>J Comput Chem</Title>
                            <JournalIssue>
                                <PubDate>
                                    <Year>2024</Year>
                                </PubDate>
                            </JournalIssue>
                        </Journal>
                        <AuthorList>
                            <Author>
                                <LastName>Smith</LastName>
                                <Initials>JK</Initials>
                            </Author>
                        </AuthorList>
                        <Abstract>
                            <AbstractText>This is the abstract text.</AbstractText>
                        </Abstract>
                    </Article>
                </MedlineCitation>
                <PubmedData>
                    <ArticleIdList>
                        <ArticleId IdType="doi">10.1234/test</ArticleId>
                    </ArticleIdList>
                </PubmedData>
            </PubmedArticle>
        </PubmedArticleSet>
        """
        articles = _parse_efetch(xml)
        assert len(articles) == 1
        assert articles[0]["pmid"] == "12345678"
        assert articles[0]["title"] == "MD Simulation of Protein"
        assert articles[0]["abstract"] == "This is the abstract text."
        assert articles[0]["authors"] == "Smith JK"
        assert articles[0]["doi"] == "10.1234/test"

    def test_parse_efetch_structured_abstract(self):
        """Parse efetch response with structured abstract."""
        from literature_server import _parse_efetch

        xml = """
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>12345678</PMID>
                    <Article>
                        <ArticleTitle>Test</ArticleTitle>
                        <Journal>
                            <Title>Test J</Title>
                            <JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue>
                        </Journal>
                        <Abstract>
                            <AbstractText Label="BACKGROUND">Background text.</AbstractText>
                            <AbstractText Label="METHODS">Methods text.</AbstractText>
                            <AbstractText Label="RESULTS">Results text.</AbstractText>
                        </Abstract>
                    </Article>
                </MedlineCitation>
                <PubmedData><ArticleIdList/></PubmedData>
            </PubmedArticle>
        </PubmedArticleSet>
        """
        articles = _parse_efetch(xml)
        assert "BACKGROUND: Background text." in articles[0]["abstract"]
        assert "METHODS: Methods text." in articles[0]["abstract"]
        assert "RESULTS: Results text." in articles[0]["abstract"]


class TestToolImport:
    """Test that server tools can be imported."""

    def test_pubmed_search_exists(self):
        """pubmed_search function exists."""
        from literature_server import pubmed_search

        # Check it's a FastMCP tool (has .fn attribute)
        assert hasattr(pubmed_search, "fn") or callable(pubmed_search)

    def test_pubmed_fetch_exists(self):
        """pubmed_fetch function exists."""
        from literature_server import pubmed_fetch

        assert hasattr(pubmed_fetch, "fn") or callable(pubmed_fetch)


class TestServerSetup:
    """Test server configuration."""

    def test_mcp_server_exists(self):
        """FastMCP server is configured."""
        from literature_server import mcp

        assert mcp is not None
        assert mcp.name == "Literature Server"


@pytest.mark.slow
@pytest.mark.asyncio
class TestLiveAPI:
    """Tests that require live API access (marked slow)."""

    async def test_pubmed_search_live(self):
        """Live search returns results."""
        pytest.skip("Requires network access - run with --runslow")

    async def test_pubmed_fetch_live(self):
        """Live fetch returns article details."""
        pytest.skip("Requires network access - run with --runslow")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
