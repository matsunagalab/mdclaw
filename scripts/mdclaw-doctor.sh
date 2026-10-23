#!/usr/bin/env bash
# Read-only diagnosis of the same runtime used by bin/mdclaw.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_ROOT="${CLAUDE_PLUGIN_ROOT:-$REPO_ROOT}"
source "$PKG_ROOT/scripts/_internal/runtime.sh"

echo "MDClaw doctor"
mdclaw_select_runtime
mdclaw_describe_runtime
mdclaw_require_runtime

status=0
case "$RUNTIME" in
    pixi|conda)
        (mdclaw_runtime_exec python -m mdclaw._cli --version) || status=1 ;;
    local)
        "$LOCAL_MDCLAW" --version || status=1 ;;
    *)
        (mdclaw_runtime_exec mdclaw --version) || status=1 ;;
esac
if [ "$status" != 0 ]; then
    echo "[fail] MDClaw CLI could not start in the selected runtime." >&2
    echo "Next: repair this runtime's MDClaw installation and rerun doctor." >&2
fi
if [ "$RUNTIME" = local ]; then
    echo "[info] Standalone CLI: dependency checks are unavailable without its Python environment."
    echo "Next: select Pixi, Conda or a container for full diagnosis; for OpenMM use mdclaw inspect_openmm_platforms."
else
    (mdclaw_runtime_exec python "$PKG_ROOT/scripts/_internal/doctor.py") || status=1
fi

echo
echo "Repository checks (independent of runtime readiness):"
REPO_ROOT="$PKG_ROOT"
if [ -x "$REPO_ROOT/bin/mdclaw" ]; then
    echo "[ok] bin/mdclaw exists"
else
    echo "[warn] bin/mdclaw is not executable"
fi

# Cloud-synced checkouts (Drive, Dropbox) can strip exec bits and symlinks.
exec_warn=0
for script in "$REPO_ROOT"/scripts/*.sh "$REPO_ROOT"/container/scripts/*.sh; do
    [ -f "$script" ] || continue
    if [ ! -x "$script" ]; then
        echo "[warn] not executable: ${script#$REPO_ROOT/}"
        exec_warn=1
    fi
done
if [ "$exec_warn" = "1" ]; then
    echo "      Restore tracked modes and symlinks with:"
    echo "      git checkout -- scripts container/scripts .agents .claude"
fi

echo
echo "Agent skills:"
skill_roots=("$REPO_ROOT/.agents/skills" "$REPO_ROOT/.claude/skills")
if [ -d "$REPO_ROOT/.codex/skills" ]; then
    skill_roots+=("$REPO_ROOT/.codex/skills")
fi
for skills_root in "${skill_roots[@]}"; do
    label="${skills_root#$REPO_ROOT/}"
    if [ -d "$skills_root" ]; then
        while IFS= read -r link; do
            if [ ! -e "$link" ]; then
                echo "[warn] broken symlink: ${link#$REPO_ROOT/} -> $(readlink "$link")"
            fi
        done < <(find "$skills_root" -maxdepth 2 -type l -print)
        missing=""
        for src in "$REPO_ROOT"/skills/*/; do
            name="$(basename "$src")"
            [ -e "$skills_root/$name" ] || missing="$missing $name"
        done
        if [ -n "$missing" ]; then
            echo "[warn] $label is missing:$missing"
            echo "      Restore the tracked mirrors: git checkout -- .agents .claude"
        fi
        if [ -e "$skills_root/common/run-loop.md" ]; then
            echo "[ok] $label/common"
        else
            echo "[warn] $label/common missing"
        fi
        found="$(find -L "$skills_root" -maxdepth 2 -name SKILL.md -print 2>/dev/null || true)"
        if [ -n "$found" ]; then
            echo "[ok] $label: $(printf '%s\n' "$found" | wc -l | tr -d ' ') skills discovered"
        else
            echo "[warn] $label contains no SKILL.md files"
        fi
    else
        echo "[warn] $label not found"
        echo "      Restore the tracked mirrors (git checkout -- .agents .claude)"
        echo "      or install skills: npx skills add matsunagalab/mdclaw"
    fi
done

exit "$status"
