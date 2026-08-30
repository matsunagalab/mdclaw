#!/usr/bin/env bash
# Install repo-local Agent Skills entrypoints for Claude, Pi, OpenCode, Codex,
# and other harnesses that discover mirrored skill directories.
#
# Skills always come from this checkout's skills/ directory. They are installed
# into the project you run the script from (the current working directory), so
# the same checkout can serve several projects; --user installs them under
# $HOME instead, for every project of that user.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="symlink"
TARGET=""

usage() {
    cat <<'USAGE'
Usage: scripts/install-agent-skills.sh [--copy] [--user | --target DIR | DIR]

Installs the skills from this checkout into a project directory: DIR if given,
otherwise the current working directory. Mirrors are written to
DIR/.agents/skills, DIR/.claude/skills, and DIR/.codex/skills.

Use --user to install into the user-level directories under $HOME instead, so
the skills are visible to every project of that user.

Default mode creates symlinks (relative inside this checkout, absolute when
installing into another project).
Use --copy for tools or filesystems that do not follow symlinks.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --copy)
            MODE="copy"
            ;;
        --user)
            [ -n "${HOME:-}" ] || { echo "--user needs HOME to be set" >&2; exit 2; }
            [ -z "$TARGET" ] || { echo "Target directory given twice" >&2; exit 2; }
            TARGET="$HOME"
            ;;
        --target)
            [ $# -ge 2 ] || { echo "--target needs a directory" >&2; exit 2; }
            [ -z "$TARGET" ] || { echo "Target directory given twice" >&2; exit 2; }
            TARGET="$2"
            shift
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            [ -z "$TARGET" ] || { echo "Target directory given twice" >&2; exit 2; }
            TARGET="$1"
            ;;
    esac
    shift
done

[ -n "$TARGET" ] || TARGET="$PWD"
[ -d "$TARGET" ] || { echo "No such directory: $TARGET" >&2; exit 2; }
INSTALL_ROOT="$(cd "$TARGET" && pwd)"

SRC_ROOT="$REPO_ROOT/skills"
DST_ROOTS=(
    "$INSTALL_ROOT/.agents/skills"
    "$INSTALL_ROOT/.claude/skills"
    "$INSTALL_ROOT/.codex/skills"
)

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
        elif [ "$INSTALL_ROOT" = "$REPO_ROOT" ]; then
            ln -s "../../skills/$name" "$dst"
        else
            ln -s "$SRC_ROOT/$name" "$dst"
        fi
    done
done

echo "Installed Agent Skills from $SRC_ROOT into $INSTALL_ROOT/{.agents,.claude,.codex}/skills ($MODE mode)."
