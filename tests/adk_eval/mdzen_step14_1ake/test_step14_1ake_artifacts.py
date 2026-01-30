import hashlib
import json
import os
from pathlib import Path

import pytest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalized_workflow_state_sha256(path: Path) -> str:
    """Hash workflow_state.json after removing volatile keys.

    This keeps the test resilient to benign changes like retry counters,
    error logs, timestamps, etc.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _sha256(path)
    except Exception:
        return _sha256(path)

    volatile_prefixes = ("_",)
    volatile_keys = {
        "last_step_summary",
        "pending_questions",
        "awaiting_user_input",
        "errors",
        "last_error",
    }
    cleaned = {}
    for k, v in data.items():
        if any(k.startswith(p) for p in volatile_prefixes):
            continue
        if k in volatile_keys:
            continue
        cleaned[k] = v
    normalized = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _collect_artifacts(job_dir: Path) -> dict:
    """Collect existence + sha256 + size for key artifacts (Step1-4)."""
    candidates = {
        "workflow_state": job_dir / "workflow_state.json",
    }

    # Best-effort patterns for expected files produced by steps
    # (these match current server implementations but may evolve).
    def newest(patterns: list[str]) -> Path | None:
        matches: list[Path] = []
        for pat in patterns:
            matches.extend(list(job_dir.glob(pat)))
        matches = [p for p in matches if p.is_file()]
        if not matches:
            return None
        return max(matches, key=lambda p: p.stat().st_mtime)

    structure = newest(["*.pdb", "*.cif"])
    merged = newest(["**/merge/merged.pdb", "**/merged.pdb", "**/merge/merged*.pdb"])
    solvated = newest(["**/solvate/solvated.pdb", "**/solvated.pdb", "**/solvate/solvated*.pdb"])

    if structure:
        candidates["structure_file"] = structure
    if merged:
        candidates["merged_pdb"] = merged
    if solvated:
        candidates["solvated_pdb"] = solvated

    out: dict[str, dict] = {}
    for key, path in candidates.items():
        if not isinstance(path, Path):
            continue
        if not path.exists():
            out[key] = {"exists": False}
            continue
        entry = {"exists": True, "sha256": _sha256(path), "size": path.stat().st_size}
        if key == "workflow_state":
            entry["normalized_sha256"] = _normalized_workflow_state_sha256(path)
        out[key] = entry
    return out


@pytest.mark.asyncio
async def test_step14_1ake_adk_eval_and_artifacts(tmp_path: Path):
    """Run ADK evaluation and validate artifacts + (optional) baseline match.

    Baseline behavior:
    - If baseline exists, compare.
    - If baseline missing or MDZEN_RECORD_BASELINE=1, write baseline.
    """
    # Keep this test model-agnostic: user can pin qwen via env before running pytest.
    os.environ.setdefault("MDZEN_USE_SCRATCHPAD", "true")
    os.environ.setdefault("MDZEN_USE_SIMPLE_PROMPT", "true")
    # Strict by default: fallback must NOT be used for a meaningful LLM test.
    os.environ.setdefault("MDZEN_EVAL_ALLOW_FALLBACK", "0")

    try:
        from google.adk.evaluation.agent_evaluator import AgentEvaluator
    except ModuleNotFoundError:
        pytest.skip('google-adk eval extras not installed. Install with: pip install "google-adk[eval]" rouge-score')

    case_dir = Path(__file__).resolve().parent
    dataset = case_dir / "step14_1ake.test.json"

    try:
        # NOTE: `AgentEvaluator.evaluate()` in this ADK version auto-constructs criteria with a
        # legacy `match_type` string. We instead call `evaluate_eval_set` directly with an
        # explicit EvalConfig. (We keep tool-trajectory evaluation for `adk eval` CLI runs.)
        from google.adk.evaluation.agent_evaluator import EvalConfig

        eval_config = EvalConfig(criteria={})
        eval_set = AgentEvaluator._load_eval_set_from_file(  # pyright: ignore[reportPrivateUsage]
            str(dataset),
            eval_config=eval_config,
            initial_session={},
        )
        await AgentEvaluator.evaluate_eval_set(
            agent_module="tests.adk_eval.mdzen_step14_1ake",
            eval_set=eval_set,
            eval_config=eval_config,
            num_runs=1,
            print_detailed_results=False,
        )
    except ModuleNotFoundError:
        pytest.skip(
            'ADK evaluation dependencies missing. Install with: pip install "google-adk[eval]" rouge-score'
        )

    # WorkflowDriverAgent persists under a stable case directory (ADK eval may not
    # preserve the same session object across turns).
    job_dir = Path("outputs") / "adk_eval" / "mdzen_step14_1ake" / "run"
    assert job_dir.exists(), f"Job dir not found: {job_dir}"

    artifacts = _collect_artifacts(job_dir)
    # Must at least have workflow_state + merged_pdb + solvated_pdb
    assert artifacts.get("workflow_state", {}).get("exists") is True
    assert artifacts.get("merged_pdb", {}).get("exists") is True
    assert artifacts.get("solvated_pdb", {}).get("exists") is True

    # Fail if evaluation fallback was used (otherwise any model could "pass").
    wf = json.loads((job_dir / "workflow_state.json").read_text(encoding="utf-8"))
    assert not wf.get("_fallback_used"), "Fallback was used; this is not a valid LLM pass."

    baseline_dir = case_dir / "baselines"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = baseline_dir / "step14_1ake.json"

    record = os.environ.get("MDZEN_RECORD_BASELINE", "").strip().lower() in {"1", "true", "yes", "on"}
    if record or not baseline_path.exists():
        baseline_path.write_text(json.dumps(artifacts, indent=2, ensure_ascii=False), encoding="utf-8")
        return

    expected = json.loads(baseline_path.read_text(encoding="utf-8"))
    # Compare only sha256+size for keys that exist in both.
    for key in ["workflow_state", "merged_pdb", "solvated_pdb"]:
        assert key in expected, f"Missing key in baseline: {key}"
        assert artifacts[key]["exists"] is True
        assert expected[key]["exists"] is True
        if key == "workflow_state":
            # If baseline is from an older schema, re-record.
            if "normalized_sha256" not in expected[key]:
                baseline_path.write_text(
                    json.dumps(artifacts, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                return
            assert (
                artifacts[key]["normalized_sha256"] == expected[key]["normalized_sha256"]
            ), f"Workflow state mismatch for {key}"
        else:
            assert artifacts[key]["size"] == expected[key]["size"], f"Size mismatch for {key}"
            assert artifacts[key]["sha256"] == expected[key]["sha256"], f"Hash mismatch for {key}"

