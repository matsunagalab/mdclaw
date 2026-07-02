"""Re-index existing membrane patch cache entries to the current fingerprint.

The patch fingerprint schema occasionally changes (e.g. v1 -> v2 dropped the
packmol-memgen version from the hashed payload). The equilibrated patch itself
is unchanged, so there is no need to re-run the expensive cold build; the entry
only needs to move to the directory named by the recomputed fingerprint.

For each ``<root>/<hh>/<hash>/manifest.json`` this recomputes the fingerprint
from the manifest parameters, and if it differs, moves the entry to the new
path and refreshes the manifest ``fingerprint`` / ``schema_version`` fields.
The ``patch.pdb`` bytes (and thus ``patch_sha256``) are preserved.

Usage:
    conda run -n mdclaw python scripts/reindex_membrane_cache.py \
        --root mdclaw/data/membrane_patches
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _reindex_one(entry_dir: Path, root: Path) -> tuple[str, str] | None:
    from mdclaw.solvation.constants import PATCH_CACHE_SCHEMA_VERSION
    from mdclaw.solvation.patch_membrane import (
        membrane_patch_fingerprint,
        patch_cache_entry_dir,
    )

    manifest_path = entry_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    params = manifest.get("parameters", {})

    new_fp, _payload = membrane_patch_fingerprint(
        lipids=params["lipids"],
        ratio=params["ratio"],
        water_model=params["water_model"],
        salt=params["salt"],
        salt_c=params["salt_c"],
        salt_a=params["salt_a"],
        saltcon=params["saltcon"],
        dist_wat=params["dist_wat"],
        leaflet=params["leaflet"],
        patch_side=params["patch_side"],
        nloop=params["nloop"],
        nloop_all=params["nloop_all"],
        equil_nvt_ns=params["equil_nvt_ns"],
        equil_npt_ns=params["equil_npt_ns"],
        equil_temperature_k=params["equil_temperature_k"],
        equil_pressure_bar=params["equil_pressure_bar"],
        forcefield=params["forcefield"],
        tolerance=params.get("tolerance", 2.0),
    )

    old_fp = manifest.get("fingerprint", entry_dir.name)
    if new_fp == old_fp and entry_dir.name == new_fp:
        return None

    new_dir = patch_cache_entry_dir(root, new_fp)
    if new_dir.resolve() == entry_dir.resolve():
        return None
    if new_dir.exists():
        raise SystemExit(f"target already exists: {new_dir}")

    # Refresh provenance fields on the in-place manifest first.
    manifest["fingerprint"] = new_fp
    manifest["schema_version"] = PATCH_CACHE_SCHEMA_VERSION
    params["schema_version"] = PATCH_CACHE_SCHEMA_VERSION
    params.pop("packmol_memgen_version", None)
    manifest["parameters"] = params
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entry_dir), str(new_dir))
    # Drop stale lock file if present.
    (new_dir / ".lock").unlink(missing_ok=True)
    return old_fp, new_fp


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path("mdclaw") / "data" / "membrane_patches"),
        help="Patch cache root to re-index.",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"[reindex] no cache root at {root}", file=sys.stderr)
        return 1

    entries = sorted(p.parent for p in root.glob("*/*/manifest.json"))
    moved = 0
    for entry in entries:
        result = _reindex_one(entry, root)
        if result is None:
            print(f"[reindex] up-to-date: {entry.name[:12]}", flush=True)
            continue
        old_fp, new_fp = result
        print(f"[reindex] moved {old_fp[:12]} -> {new_fp[:12]}", flush=True)
        moved += 1

    # Prune now-empty shard directories.
    for shard in list(root.glob("*")):
        if shard.is_dir() and not any(shard.iterdir()):
            shard.rmdir()

    print(f"[reindex] done: {moved} moved, {len(entries) - moved} unchanged.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
