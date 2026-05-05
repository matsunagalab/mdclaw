# Release Workflow

Skills and the containerized tools ship through separate channels. Keep the
plugin metadata and Python package version in sync.

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

# 3. Build and test the image
docker build -f container/Dockerfile -t mdclaw:latest .
docker run --rm --gpus all -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh

# 4. Push versioned and latest images
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
