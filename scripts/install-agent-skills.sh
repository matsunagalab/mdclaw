#!/usr/bin/env bash
# Install repo-local Agent Skills entrypoints for Pi, OpenCode, Codex, and
# other harnesses that discover `.agents/skills/<name>/SKILL.md`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="symlink"

if [ "${1:-}" = "--copy" ]; then
    MODE="copy"
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'USAGE'
Usage: scripts/install-agent-skills.sh [--copy]

Default mode creates relative symlinks under .agents/skills.
Use --copy for tools or filesystems that do not follow symlinks.
USAGE
    exit 0
fi

SRC_ROOT="$REPO_ROOT/skills"
DST_ROOT="$REPO_ROOT/.agents/skills"
mkdir -p "$DST_ROOT"

for skill_dir in "$SRC_ROOT"/*; do
    [ -d "$skill_dir" ] || continue
    [ -f "$skill_dir/SKILL.md" ] || continue
    name="$(basename "$skill_dir")"
    dst="$DST_ROOT/$name"
    rm -rf "$dst"
    if [ "$MODE" = "copy" ]; then
        mkdir -p "$dst"
        cp -R "$skill_dir"/. "$dst"/
    else
        ln -s "../../skills/$name" "$dst"
    fi
done

echo "Installed Agent Skills in $DST_ROOT ($MODE mode)."
