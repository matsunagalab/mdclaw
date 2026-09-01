#!/usr/bin/env bash
# Install Agent Skills entrypoints for Claude, Pi, OpenCode, Codex, and other
# harnesses that discover mirrored skill directories.
#
# Skills always come from this checkout's skills/. They are installed into the
# project you run the script from, so one checkout can serve several projects.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="symlink"

if [ "${1:-}" = "--copy" ]; then
    MODE="copy"
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'USAGE'
Usage: scripts/install-agent-skills.sh [--copy]

Installs into the current directory: .agents/skills, .claude/skills, and
.codex/skills. Run it in this checkout to wire up the checkout itself, or in
another project to serve that project from this checkout.

Default mode creates symlinks, relative inside this checkout and absolute
elsewhere.
Use --copy for tools or filesystems that do not follow symlinks.
USAGE
    exit 0
fi

SRC_ROOT="$REPO_ROOT/skills"
INSTALL_ROOT="$PWD"
DST_ROOTS=(
    "$INSTALL_ROOT/.agents/skills"
    "$INSTALL_ROOT/.claude/skills"
    "$INSTALL_ROOT/.codex/skills"
)
# The mirrors inside this checkout are committed, so they must stay relative;
# mirrors in another project can only point back here by absolute path.
if [ "$INSTALL_ROOT" = "$REPO_ROOT" ]; then
    LINK_ROOT="../../skills"
else
    LINK_ROOT="$SRC_ROOT"
fi

for dst_root in "${DST_ROOTS[@]}"; do
    mkdir -p "$dst_root"
    while IFS= read -r link; do
        [ -e "$link" ] || rm -f "$link"
    done < <(find "$dst_root" -maxdepth 1 -type l -print)
done

for skill_dir in "$SRC_ROOT"/*; do
    [ -d "$skill_dir" ] || continue
    name="$(basename "$skill_dir")"
    for dst_root in "${DST_ROOTS[@]}"; do
        dst="$dst_root/$name"
        rm -rf "$dst"
        if [ "$MODE" = "copy" ]; then
            mkdir -p "$dst"
            cp -R "$skill_dir"/. "$dst"/
        else
            ln -s "$LINK_ROOT/$name" "$dst"
        fi
    done
done

echo "Installed Agent Skills from $SRC_ROOT into $INSTALL_ROOT/{.agents,.claude,.codex}/skills ($MODE mode)."
