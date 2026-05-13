# Release Workflow

Skills and the containerized MD runtime ship through separate channels. Keep
the plugin metadata, Python package version, and image tags in sync.

## Version Locations

| File | Field |
|------|-------|
| `mdclaw/__init__.py` | `__version__` |
| `pyproject.toml` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `metadata.version` and `plugins[0].version` |

## Steps

```bash
# 1. Update all version locations to X.Y.Z

# 2. Commit, tag, and push
git add -A
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags

# 3. Build and test the packaged MD runtime image
docker build -f container/Dockerfile -t mdclaw:latest .
docker run --rm --gpus all -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh

# 4. Push versioned and latest runtime images
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker push ghcr.io/matsunagalab/mdclaw:latest
```

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
3. Provide one runtime: conda (`environment.yml`), SIF (`MDCLAW_SIF`), or Docker
   (`MDCLAW_DOCKER_IMAGE`).

Slash commands are optional. A generic harness can start by reading
`skills/md-prepare/SKILL.md` and then continue through the next `SKILL.md`
files using the same `job_dir`.
