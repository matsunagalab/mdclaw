"""Literature server package.

Behavior-preserving split of the former monolithic ``mdclaw/literature_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.literature.search import pubmed_search
from mdclaw.literature.fetch import pubmed_fetch

TOOLS = {
    "pubmed_search": pubmed_search,
    "pubmed_fetch": pubmed_fetch,
}

__all__ = [
    "pubmed_search",
    "pubmed_fetch",
    "TOOLS",
]
