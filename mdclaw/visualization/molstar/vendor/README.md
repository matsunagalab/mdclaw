---
title: Bundled Molstar assets
category: third-party
updated: 2026-10-24
---

# Mol* 5.13.0

Unmodified `build/viewer/molstar.js` and `build/viewer/molstar.css` from the
official npm `molstar@5.13.0` tarball, stored with deterministic gzip
compression (`gzip.compress(data, mtime=0)`). Served with `Content-Encoding:
gzip`. `LICENSE` is the upstream MIT licence; third-party notices embedded
in the bundle are retained. `provenance.json` records the tarball URL, npm
SHA-512 integrity, and raw/compressed SHA-256 hashes.

Updates are manual: choose an upstream release, verify the npm tarball
integrity, extract only these named assets and the licence, recompress,
and refresh provenance plus the version in the Python viewer and tests.
Run the HTTP/CLI tests and verify PDB/DCD playback in both supported hosts.
The bundle uses generated functions, so the viewer CSP permits
`unsafe-eval` for its same-origin scripts. External scripts and connections
remain blocked. No npm install or build runs on the user's machine.
