"""Deployment and developer docs reference real repository paths.

README.md once pointed at a renamed test file and a benchmark tree that had
moved to its own repository; this test catches that class of drift. It checks
backtick-quoted repo paths and markdown link targets in the deploy-facing
docs against the working tree.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DOC_FILES = [
    "README.md",
    "AGENTS.md",
    "docs/agents/deployment.md",
    "docs/developer/container.md",
    "docs/developer/release.md",
]

# Backtick tokens are checked only under tracked top-level directories;
# URLs, absolute paths, and study/job layout examples are out of scope.
KNOWN_ROOTS = (
    "mdclaw/", "skills/", "docs/", "tests/", "scripts/", "container/",
    "hooks/", "bin/", "examples/", "benchmarks/",
    ".claude-plugin/", ".agents/", ".claude/",
)

PLACEHOLDER_CHARS = set("*<>{}$()|")


def _candidate_tokens(text: str):
    # Inline `code` spans, plus every whitespace-separated word inside fenced
    # code blocks (commands like `pytest tests/test_x.py` live there).
    for token in re.findall(r"`([^`\n]+)`", text):
        yield token
    for block in re.findall(r"^```.*?\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
        yield from block.split()


def _backtick_paths(text: str):
    for token in _candidate_tokens(text):
        token = token.strip().strip("\"'").rstrip(".,;:\\")
        if "/" not in token or " " in token:
            continue
        if PLACEHOLDER_CHARS & set(token):
            continue
        if token.startswith(KNOWN_ROOTS):
            yield token


def _link_targets(text: str):
    for target in re.findall(r"\]\(([^)#\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        yield target


def test_docs_reference_existing_repo_paths():
    missing = []
    for doc_name in DOC_FILES:
        doc = REPO_ROOT / doc_name
        text = doc.read_text()
        for token in sorted(set(_backtick_paths(text))):
            if not (REPO_ROOT / token).exists():
                missing.append(f"{doc_name}: `{token}`")
        # Markdown links resolve relative to the doc; repo-root-relative
        # targets are tolerated because README links are written that way.
        for target in sorted(set(_link_targets(text))):
            if not (doc.parent / target).exists() \
                    and not (REPO_ROOT / target).exists():
                missing.append(f"{doc_name}: ({target})")
    assert not missing, "Docs reference missing paths:\n" + "\n".join(missing)
