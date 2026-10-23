# Release Workflow

Skills and the containerized MD runtime ship through separate channels. Keep
the plugin metadata, Python package version, Pi package version, and image tags
in sync.

## Version Locations

| File | Field |
|------|-------|
| `mdclaw/__init__.py` | `__version__` |
| `pyproject.toml` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `metadata.version` and `plugins[0].version` |
| `package.json` | `version` |

## Steps

```bash
# 1. Write X.Y.Z to all version locations at once, then verify the sync
scripts/bump-version.sh X.Y.Z
scripts/mdclaw-doctor.sh

# 2. Commit, tag, and push. The v-tag triggers .github/workflows/container.yml:
#    it builds the amd64 image from the committed pixi.lock, runs the CPU
#    smoke test (container/scripts/test-container.sh), and pushes
#    ghcr.io/matsunagalab/mdclaw:X.Y.Z and :latest. The workflow fails if the
#    tag does not match the plugin.json version or if pixi.lock is stale.
git add -A
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags

# 3. GPU smoke test — a manual gate (hosted runners have no GPU). Run on a
#    GPU host before replacing any shared SIF:
docker run --rm --gpus all \
  -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  ghcr.io/matsunagalab/mdclaw:X.Y.Z bash /work/test.sh
```

To rebuild and validate without cutting a release, run the Container workflow
manually (`workflow_dispatch`); it pushes only when `push_image` is set. The
old manual build-and-push commands remain in `docs/developer/container.md` as
the fallback when Actions is unavailable.

One-time setup: the GHCR package `mdclaw` must grant this repository's
Actions write access (package settings → Manage Actions access) or the
`GITHUB_TOKEN` push is denied — the existing package was first pushed with a
personal token, so it is not linked to the repository automatically.

Users update skills and the wrapper with:

```text
/plugin update mdclaw@mdclaw
```

The session-start hook downloads the matching SIF on the next session start.

## Generic Harness Deployment

For harnesses without MDClaw plugin support, keep deployment simple:

1. Install or check out this repository so the harness can read `skills/`.
2. Put `bin/mdclaw` on `PATH`, or install the Python package and expose the
   `mdclaw` CLI.
3. Provide one runtime: Pixi (`pixi install`), a shared SIF, Docker, or an
   existing Conda environment. Follow [deployment](../agents/deployment.md)
   for explicit runtime selection and diagnosis.

Slash commands are optional. A generic harness can start by reading
`skills/md-prepare/SKILL.md` and then continue through the next `SKILL.md`
files using the same `job_dir`.
