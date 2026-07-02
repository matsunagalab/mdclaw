#!/usr/bin/env bash
# Write a new version to every MDClaw version location at once.
#
# Version locations (kept in sync; verified by scripts/mdclaw-doctor.sh):
#   - mdclaw/__init__.py            __version__
#   - pyproject.toml                [project].version
#   - .claude-plugin/plugin.json    version
#   - .claude-plugin/marketplace.json  metadata.version and plugins[0].version
#   - package.json                  version
#
# Usage:
#   scripts/bump-version.sh X.Y.Z
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -ne 1 ]; then
    echo "Usage: $0 X.Y.Z" >&2
    exit 2
fi

VERSION="$1"
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$'; then
    echo "Error: '$VERSION' does not look like a semantic version (X.Y.Z)." >&2
    exit 2
fi

python3 - "$REPO_ROOT" "$VERSION" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]


def sub_once(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text()
    new_text, n = re.subn(pattern, repl, text, count=1)
    if n != 1:
        raise SystemExit(f"Failed to update version in {path} (matched {n} times)")
    path.write_text(new_text)


# mdclaw/__init__.py: __version__ = "X.Y.Z"
sub_once(
    root / "mdclaw" / "__init__.py",
    r'__version__\s*=\s*"[^"]+"',
    f'__version__ = "{version}"',
)

# pyproject.toml: version = "X.Y.Z" (first occurrence, under [project])
sub_once(
    root / "pyproject.toml",
    r'(?m)^version\s*=\s*"[^"]+"',
    f'version = "{version}"',
)

# JSON files: regex-replace the version string in place so existing formatting
# (array wrapping, key order) is preserved. Validate the result parses.
def sub_json_version(path: Path, count: int) -> None:
    text = path.read_text()
    new_text, n = re.subn(
        r'"version"\s*:\s*"[^"]+"',
        f'"version": "{version}"',
        text,
        count=count,
    )
    if n != count:
        raise SystemExit(f"Expected {count} version field(s) in {path}, updated {n}")
    json.loads(new_text)  # fail loudly on malformed output
    path.write_text(new_text)


# plugin.json has one version; package.json has one; marketplace.json has two
# (metadata.version and plugins[0].version), both of which must move together.
sub_json_version(root / ".claude-plugin" / "plugin.json", count=1)
sub_json_version(root / "package.json", count=1)
sub_json_version(root / ".claude-plugin" / "marketplace.json", count=2)

print(f"Updated all version locations to {version}")
PY

echo
echo "Verify with: scripts/mdclaw-doctor.sh"
