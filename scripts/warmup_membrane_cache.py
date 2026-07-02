"""Pre-build representative membrane patches into a bundled patch cache.

The patch-tile membrane backend caches a small, PBC-equilibrated membrane patch
per composition.  The first use of a new composition pays a one-time cold build
(packmol pack + OpenMM minimize + short equilibration).  Running this script
during image builds pre-populates a read-only cache so common compositions hit
at runtime instead of triggering that cold build.

Runtime lookup of the bundled cache is controlled by
``MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR`` (see
``mdclaw.solvation.patch_membrane.resolve_bundled_patch_cache_roots``).  The
packaged default location is ``mdclaw/data/membrane_patches``.

Usage:
    conda run -n mdclaw python scripts/warmup_membrane_cache.py \
        --out mdclaw/data/membrane_patches
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# Representative compositions to warm.  Keep in sync with the skills docs and
# packmol-memgen's example compositions; each entry is (lipids, ratio).
DEFAULT_COMPOSITIONS: list[tuple[str, str]] = [
    ("POPC", "1"),
    ("POPE", "1"),
    ("DPPC", "1"),
    ("DOPC", "1"),
    ("POPC:CHL1", "4:1"),
    ("POPC:POPE:CHL1", "2:1:1"),  # MDPrepBench P18
    ("DOPE:DOPG", "3:1"),
    ("DPPC:DOPC:CHL1", "1:1:1"),  # raft-like
]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(Path("mdclaw") / "data" / "membrane_patches"),
        help="Writable patch cache root to populate (default: mdclaw/data/membrane_patches).",
    )
    parser.add_argument(
        "--water-model", default="opc", help="Water model for the patch (default: opc)."
    )
    parser.add_argument(
        "--saltcon", type=float, default=0.15, help="Salt concentration in M (default: 0.15)."
    )
    parser.add_argument(
        "--compositions",
        nargs="*",
        default=None,
        help="Override compositions as 'lipids=ratio' items, e.g. POPC=1 POPC:CHL1=4:1.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first composition that fails to build.",
    )
    return parser.parse_args(argv)


def _resolve_compositions(raw: list[str] | None) -> list[tuple[str, str]]:
    if not raw:
        return DEFAULT_COMPOSITIONS
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"Invalid composition {item!r}; expected 'lipids=ratio'.")
        lipids, ratio = item.split("=", 1)
        parsed.append((lipids.strip(), ratio.strip()))
    return parsed


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    from mdclaw.solvation.constants import (
        PATCH_EQUIL_FORCEFIELD,
        PATCH_NLOOP,
        PATCH_NLOOP_ALL,
        PATCH_SIDE_ANGSTROM,
        patch_equilibration_params,
    )
    from mdclaw.solvation.patch_membrane import ensure_membrane_patch
    from mdclaw.solvation_server import (
        _equilibrate_membrane_patch,
        _packmol_memgen_version,
        _resolve_patch_builder_timeout,
        _run_packmol_memgen_noninteractive,
    )

    if not shutil.which("packmol-memgen"):
        print("[warmup] packmol-memgen not found in PATH; cannot warm patches.", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    compositions = _resolve_compositions(args.compositions)
    equil_params = {
        **patch_equilibration_params(),
        "water_model": args.water_model,
        "forcefield": PATCH_EQUIL_FORCEFIELD,
    }
    version = _packmol_memgen_version()
    timeout = _resolve_patch_builder_timeout(None)

    failures: list[str] = []
    for lipids, ratio in compositions:
        label = f"{lipids} ({ratio})"
        print(f"[warmup] building patch: {label}", flush=True)
        started = time.time()
        result = ensure_membrane_patch(
            lipids=lipids,
            ratio=ratio,
            water_model=args.water_model,
            salt=True,
            salt_c="Na+",
            salt_a="Cl-",
            saltcon=args.saltcon,
            dist_wat=17.5,
            leaflet=23.0,
            patch_side=PATCH_SIDE_ANGSTROM,
            nloop=PATCH_NLOOP,
            nloop_all=PATCH_NLOOP_ALL,
            equil_params=equil_params,
            forcefield=PATCH_EQUIL_FORCEFIELD,
            cache_mode="auto",
            cache_dir=str(out_root),
            packmol_memgen_runner=_run_packmol_memgen_noninteractive,
            packmol_path=shutil.which("packmol"),
            equilibrate_fn=_equilibrate_membrane_patch,
            timeout=timeout,
            packmol_memgen_version=version,
        )
        elapsed = time.time() - started
        if result.get("success"):
            state = "cache-hit" if result.get("cache_hit") else "built"
            print(
                f"[warmup] ok: {label} [{state}] fingerprint={result['fingerprint'][:12]} "
                f"({elapsed:.0f}s)",
                flush=True,
            )
        else:
            msg = "; ".join(result.get("errors", [])) or result.get("code", "unknown error")
            print(f"[warmup] FAILED: {label}: {msg}", file=sys.stderr, flush=True)
            failures.append(label)
            if args.fail_fast:
                return 1

    if failures:
        print(f"[warmup] {len(failures)} composition(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"[warmup] done. Patch cache populated at {out_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
