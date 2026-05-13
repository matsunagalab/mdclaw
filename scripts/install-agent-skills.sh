#!/usr/bin/env bash
# Install repo-local Agent Skills entrypoints for Claude, Pi, OpenCode, Codex,
# and other harnesses that discover mirrored skill directories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="symlink"

if [ "${1:-}" = "--copy" ]; then
    MODE="copy"
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'USAGE'
Usage: scripts/install-agent-skills.sh [--copy]

Default mode creates relative symlinks under .agents/skills and .claude/skills.
Use --copy for tools or filesystems that do not follow symlinks.
USAGE
    exit 0
fi

SRC_ROOT="$REPO_ROOT/skills"
DST_ROOTS=(
    "$REPO_ROOT/.agents/skills"
    "$REPO_ROOT/.claude/skills"
)

for dst_root in "${DST_ROOTS[@]}"; do
    mkdir -p "$dst_root"
done

for skill_dir in "$SRC_ROOT"/*; do
    [ -d "$skill_dir" ] || continue
    [ -f "$skill_dir/SKILL.md" ] || continue
    name="$(basename "$skill_dir")"
    for dst_root in "${DST_ROOTS[@]}"; do
        dst="$dst_root/$name"
        rm -rf "$dst"
        if [ "$MODE" = "copy" ]; then
            mkdir -p "$dst"
            cp -R "$skill_dir"/. "$dst"/
        else
            ln -s "../../skills/$name" "$dst"
        fi
    done
done

echo "Installed Agent Skills in .agents/skills and .claude/skills ($MODE mode)."
