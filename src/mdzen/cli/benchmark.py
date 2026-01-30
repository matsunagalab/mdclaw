"""Benchmark CLI for MDZen.

Runs predefined benchmark cases multiple times and reports success rate.

Design notes:
- We execute each attempt in a **subprocess** calling `main.py run -p ...`.
  This is important because `main.py` intentionally uses `os._exit(0)` to
  suppress MCP async generator cleanup noise, which would terminate an in-process
  benchmark runner.
- We treat `session_dir/validation_result.json` as the machine-readable ground
  truth for success/failure (produced by Phase 3 validation).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml


benchmark_app = typer.Typer(help="Benchmark runner for MDZen (repeat cases, compute success rate)")


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    name: str
    prompt: str
    expected_artifacts: list[str]
    repeats: int
    pdb_id: str | None = None
    structure_format: str | None = None  # pdb or cif
    pinned_structure_relpath: str | None = None
    pinned_prompt: str | None = None


def _project_root() -> Path:
    # Resolve by walking up from this file to find pyproject.toml
    cur = Path(__file__).resolve()
    for parent in [cur.parent] + list(cur.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: assume cwd is project root
    return Path.cwd()


def _load_cases(cases_path: Path) -> list[BenchmarkCase]:
    data = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    raw_cases = data.get("cases", []) if isinstance(data, dict) else []
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}

    cases: list[BenchmarkCase] = []
    for c in raw_cases:
        if not isinstance(c, dict):
            continue
        case_id = str(c.get("id", "")).strip()
        if not case_id:
            continue
        repeats = int(c.get("repeats", defaults.get("repeats", 10)))
        expected = c.get("expected_artifacts", defaults.get("expected_artifacts", ["parm7", "rst7"]))
        expected_artifacts = [str(x) for x in (expected or [])]
        cases.append(
            BenchmarkCase(
                id=case_id,
                name=str(c.get("name", case_id)),
                prompt=str(c.get("prompt", "")).strip(),
                expected_artifacts=expected_artifacts,
                repeats=repeats,
                pdb_id=(str(c.get("pdb_id")).strip().upper() if c.get("pdb_id") else None),
                structure_format=(str(c.get("format")).strip().lower() if c.get("format") else None),
                pinned_structure_relpath=(
                    str(c.get("pinned_structure_relpath")).strip()
                    if c.get("pinned_structure_relpath")
                    else None
                ),
                pinned_prompt=(str(c.get("pinned_prompt")).strip() if c.get("pinned_prompt") else None),
            )
        )
    return cases


def _find_single_job_dir(output_dir: Path) -> Path | None:
    job_dirs = sorted([p for p in output_dir.glob("job_*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    if len(job_dirs) == 1:
        return job_dirs[0]
    if len(job_dirs) > 1:
        # Prefer the newest
        return job_dirs[-1]
    return None


def _read_validation_json(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "validation_result.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _prefetch_pdb_to_cache(pdb_id: str, fmt: str, cache_dir: Path) -> Path | None:
    """Download PDB/mmCIF into cache if missing, return cached path.

    This is optional prefetch. The Research MCP server also maintains its own cache,
    but prefetching makes failures easier to diagnose and improves determinism.
    """
    import httpx

    pdb_id = pdb_id.upper()
    fmt = fmt.lower()
    if fmt not in ("pdb", "cif"):
        fmt = "cif"

    cache_entry = cache_dir / "pdb" / pdb_id
    cache_entry.mkdir(parents=True, exist_ok=True)
    cached = cache_entry / f"{pdb_id}.{fmt}"
    meta = cache_entry / "metadata.json"

    if cached.exists():
        return cached

    url = f"https://files.rcsb.org/download/{pdb_id}.{fmt}"
    try:
        r = httpx.get(url, timeout=30.0)
        if r.status_code != 200:
            return None
        cached.write_bytes(r.content)
        sha256 = _sha256_file(cached)

        # Lightweight structural inspection (gemmi) for pinning metadata
        inspection: dict[str, Any] = {}
        try:
            import gemmi

            if fmt == "cif":
                doc = gemmi.cif.read(str(cached))
                st = gemmi.make_structure_from_block(doc[0])
            else:
                st = gemmi.read_pdb(str(cached))
            st.setup_entities()
            model = st[0]
            inspection["chains"] = list(dict.fromkeys(ch.name for ch in model))
            # Collect heterogens (very rough): residue names in HETATM that are not water
            water = {"HOH", "WAT", "H2O", "DOD", "D2O"}
            het = set()
            for chain in model:
                for res in chain:
                    if res.het_flag and res.name not in water:
                        het.add(res.name)
            inspection["heterogens"] = sorted(list(het))
        except Exception:
            inspection = {}

        meta.write_text(
            json.dumps(
                {
                    "pdb_id": pdb_id,
                    "file_format": fmt,
                    "source_url": url,
                    "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sha256": sha256,
                    "inspection": inspection,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return cached
    except Exception:
        return None


def _maybe_materialize_boltz_pin(case: BenchmarkCase, job_dir: Path | None, cache_dir: Path) -> Path | None:
    """If this is a Boltz case, copy a predicted structure into cache (pin by file hash)."""
    if not case.pinned_structure_relpath or not job_dir:
        return None

    pinned_abs = cache_dir / case.pinned_structure_relpath
    if pinned_abs.exists():
        return pinned_abs

    candidates = []
    for pattern in [
        "**/boltz/**/predictions/*.cif",
        "**/boltz/**/predictions/*.pdb",
        "**/boltz/**/*.cif",
        "**/boltz/**/*.pdb",
    ]:
        candidates.extend(job_dir.glob(pattern))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None

    src = max(candidates, key=lambda p: p.stat().st_mtime)
    pinned_abs.parent.mkdir(parents=True, exist_ok=True)
    pinned_abs.write_bytes(src.read_bytes())
    (pinned_abs.parent / "metadata.json").write_text(
        json.dumps(
            {
                "case_id": case.id,
                "source_job_dir": str(job_dir),
                "source_file": str(src),
                "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sha256": _sha256_file(pinned_abs),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return pinned_abs


@benchmark_app.command("run")
def run_benchmarks(
    cases_file: Path = typer.Option(
        None,
        "--cases",
        help="Path to benchmark cases YAML (default: benchmarks/cases_v1.yaml)",
    ),
    case_id: str | None = typer.Option(
        None,
        "--case",
        help="Run only a single case by id (default: run all cases)",
    ),
    repeats: int | None = typer.Option(
        None,
        "--repeats",
        help="Override repeats for all selected cases",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Model alias/name passed to `main.py run --model` (e.g., claude-sonnet, gpt-4o-mini)",
    ),
    out_dir: Path = typer.Option(
        None,
        "--out",
        help="Benchmark results output dir (default: benchmarks/runs/<timestamp>)",
    ),
    cache_dir: Path = typer.Option(
        None,
        "--cache-dir",
        help="Cache dir for pinned downloads (default: .mdzen_cache in repo root)",
    ),
    prefetch: bool = typer.Option(
        True,
        "--prefetch/--no-prefetch",
        help="Prefetch PDB structures into cache before running attempts (recommended)",
    ),
):
    """Run benchmark cases and summarize success rate.

    This command does NOT require interactive input; it uses `main.py run -p`.
    """
    root = _project_root()
    if cases_file is None:
        cases_file = root / "benchmarks" / "cases_v1.yaml"
    if out_dir is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = root / "benchmarks" / "runs" / ts
    if cache_dir is None:
        cache_dir = root / ".mdzen_cache"

    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cases = _load_cases(cases_file)
    if case_id:
        cases = [c for c in cases if c.id == case_id]
    if not cases:
        raise typer.Exit(code=2)

    summary: list[dict[str, Any]] = []
    for case in cases:
        # Optional: prefetch PDB into cache to reduce run-to-run variability.
        if prefetch and case.pdb_id:
            _prefetch_pdb_to_cache(case.pdb_id, case.structure_format or "cif", cache_dir)

        case_repeats = repeats if repeats is not None else case.repeats
        for attempt_idx in range(1, case_repeats + 1):
            attempt_dir = out_dir / case.id / f"attempt_{attempt_idx:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            # One attempt -> one output base, so we can find the job dir deterministically.
            env["MDZEN_OUTPUT_DIR"] = str(attempt_dir)
            env["MDZEN_CACHE_DIR"] = str(cache_dir)
            env.setdefault("MDZEN_LOG_LEVEL", "WARNING")

            # For Boltz cases: if pinned structure exists, use pinned_prompt to avoid re-predicting.
            prompt = case.prompt
            if case.pinned_structure_relpath and case.pinned_prompt:
                pinned_abs = cache_dir / case.pinned_structure_relpath
                if pinned_abs.exists():
                    prompt = case.pinned_prompt.format(structure_file=str(pinned_abs))

            cmd = [
                sys.executable,
                str(root / "main.py"),
                "run",
                "--print",
            ]
            if model:
                cmd.extend(["--model", model])
            cmd.append(prompt)

            start = time.time()
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
            duration_s = time.time() - start

            job_dir = _find_single_job_dir(attempt_dir)
            validation = _read_validation_json(job_dir) if job_dir else None

            # If this was a Boltz case and we don't have a pin yet, try to pin now.
            _maybe_materialize_boltz_pin(case, job_dir, cache_dir)

            success = False
            if validation and isinstance(validation, dict):
                success = bool(validation.get("success", False))
            else:
                # If validation_result.json is missing, treat as failure.
                # This prevents false positives when a run crashes early or exits abruptly.
                success = False

            row = {
                "case_id": case.id,
                "case_name": case.name,
                "attempt": attempt_idx,
                "repeats": case_repeats,
                "exit_code": proc.returncode,
                "duration_s": round(duration_s, 3),
                "attempt_dir": str(attempt_dir),
                "job_dir": str(job_dir) if job_dir else "",
                "success": success,
                "validation_path": str((job_dir / "validation_result.json")) if job_dir else "",
            }
            # Keep stderr/stdout for debugging but avoid huge files by truncation.
            row["stdout_head"] = proc.stdout[:4000]
            row["stderr_head"] = proc.stderr[:4000]

            summary.append(row)

            (attempt_dir / "benchmark_metadata.json").write_text(
                json.dumps(row, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # Write overall summary
    out_summary = out_dir / "summary.json"
    out_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also write CSV (simple, dependency-free)
    try:
        import csv

        csv_path = out_dir / "summary.csv"
        fieldnames = [
            "case_id",
            "case_name",
            "attempt",
            "repeats",
            "exit_code",
            "duration_s",
            "attempt_dir",
            "job_dir",
            "success",
            "validation_path",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in summary:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
    except Exception:
        pass

    # Print a compact summary
    by_case: dict[str, dict[str, Any]] = {}
    for r in summary:
        cid = r["case_id"]
        by_case.setdefault(cid, {"case_name": r["case_name"], "total": 0, "success": 0})
        by_case[cid]["total"] += 1
        by_case[cid]["success"] += 1 if r["success"] else 0

    typer.echo(f"Wrote benchmark results to: {out_dir}")
    for cid, agg in by_case.items():
        total = agg["total"]
        ok = agg["success"]
        typer.echo(f"- {cid}: {ok}/{total} success")

