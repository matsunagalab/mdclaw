"""genesis.modeller submodule (behavior-preserving split)."""

import os
import sys
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from mdclaw._common import create_unique_subdir, generate_job_id, tail_for_agent

from mdclaw.genesis._base import (
    WORKING_DIR,
    logger,
)


def _has_modeller_license_env() -> bool:
    """Return True when the user provided a MODELLER license via env vars."""
    return any(
        key.startswith("KEY_MODELLER") and bool(str(value).strip())
        for key, value in os.environ.items()
    )


def _sanitize_modeller_code(value: str, fallback: str) -> str:
    """Make a MODELLER-safe identifier from a filename stem or user value."""
    raw = (value or fallback).strip() or fallback
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    return cleaned or fallback


def _wrap_modeller_sequence(sequence: str, width: int = 75) -> str:
    """Wrap a sequence for PIR/SEG alignment files."""
    compact = "".join(sequence.split())
    return "\n".join(compact[i:i + width] for i in range(0, len(compact), width))


def _write_modeller_seed_alignment(
    path: Path,
    *,
    target_code: str,
    target_sequence: str,
    template_code: str,
) -> None:
    """Write the minimal SEG/PIR seed file consumed by AutoModel.auto_align()."""
    wrapped_sequence = _wrap_modeller_sequence(target_sequence)
    path.write_text(
        "\n".join([
            f">P1;{target_code}",
            f"sequence:{target_code}:::::target:synthetic:-1.00:-1.00",
            f"{wrapped_sequence}*",
            f">P1;{template_code}",
            f"structureX:{template_code}:FIRST:@:LAST:@:template:synthetic:-1.00:-1.00",
            "*",
            "",
        ])
    )


def _write_modeller_runner(path: Path) -> None:
    """Write the isolated MODELLER runner script used by the wrapper tool."""
    path.write_text(
        r'''import json
import os
import re
import sys
import types
import importlib.util
from pathlib import Path

license_key = next(
    (value for key, value in os.environ.items() if key.startswith("KEY_MODELLER") and value),
    None,
)
spec = importlib.util.find_spec("modeller")
if spec is None:
    raise ModuleNotFoundError("No module named 'modeller'")

install_dir = None
search_locations = list(spec.submodule_search_locations or [])
if search_locations:
    config_path = Path(search_locations[0]) / "config.py"
    if config_path.exists():
        match = re.search(
            r"install_dir\s*=\s*r?['\"]([^'\"]+)['\"]",
            config_path.read_text(),
        )
        if match:
            install_dir = match.group(1)

if license_key:
    cfg = types.ModuleType("modeller.config")
    cfg.license = license_key
    if install_dir:
        cfg.install_dir = install_dir
    sys.modules["modeller.config"] = cfg

from modeller import Alignment, Environ, Model, Selection, log
from modeller.automodel import AutoModel, LoopModel, assess


class _MissingResidueLoopModel(LoopModel):
    """LoopModel that refines every gap loop (missing residues).

    The stock ``select_loop_atoms`` only refines gaps of length 5..15. For
    filling missing residues we want every gap loop refined regardless of
    length, so widen the window via ``loop_min_length`` / ``loop_max_length``
    from the config.
    """

    def select_loop_atoms(self):
        aln = self.read_alignment()
        loops = self.loops(
            aln,
            minlength=self.mdclaw_loop_min_length,
            maxlength=self.mdclaw_loop_max_length,
            insertion_ext=2,
            deletion_ext=1,
        )
        sel = Selection(loops).only_std_residues()
        if len(sel) == 0:
            raise RuntimeError(
                "No gap loops detected for refinement; nothing to model"
            )
        return sel


def _jsonable(value):
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


config = json.loads(Path(sys.argv[1]).read_text())
log.verbose()

if config.get("random_seed") is None:
    env = Environ()
else:
    env = Environ(rand_seed=int(config["random_seed"]))

env.io.atom_files_directory = ["."]
env.io.hetatm = bool(config.get("hetatm", False))

if config.get("multichain"):
    # Build the complex alignment from the template structure with align2d.
    # Target chains are joined with '/'; align2d aligns block-by-block so the
    # target and template must expose the same number of chains.
    aln = Alignment(env)
    segment = config.get("template_segment")
    if segment:
        tmpl = Model(
            env,
            file=config["template_code"],
            model_segment=(segment[0], segment[1]),
        )
    else:
        tmpl = Model(env, file=config["template_code"])
    aln.append_model(
        tmpl,
        align_codes=config["template_code"],
        atom_files=config["template_code"] + ".pdb",
    )
    aln.append_sequence("/".join(config["target_sequences"]))
    aln[-1].code = config["target_code"]
    aln.align2d()
    aln.write(file=config["alignment_file"], alignment_format="PIR")

if config.get("loop_refinement"):
    # Build the base comparative model, then run dedicated loop refinement
    # over every gap loop (the missing residues).
    model = _MissingResidueLoopModel(
        env,
        alnfile=config["alignment_file"],
        knowns=config["template_code"],
        sequence=config["target_code"],
        assess_methods=(assess.DOPE, assess.GA341),
    )
    model.mdclaw_loop_min_length = int(config.get("loop_min_length", 1))
    model.mdclaw_loop_max_length = int(config.get("loop_max_length", 30))
    model.starting_model = 1
    model.ending_model = int(config["num_models"])
    model.loop.starting_model = 1
    model.loop.ending_model = int(config.get("loop_models", 2))
    model.loop.assess_methods = (assess.DOPE, assess.GA341)
else:
    model = AutoModel(
        env,
        alnfile=config["alignment_file"],
        knowns=config["template_code"],
        sequence=config["target_code"],
        assess_methods=(assess.DOPE, assess.GA341),
    )
    model.starting_model = 1
    model.ending_model = int(config["num_models"])

if config.get("auto_align"):
    model.auto_align()

model.make()

# For loop refinement the deliverable models are the refined loop outputs.
raw_outputs = (
    model.loop.outputs if config.get("loop_refinement") else model.outputs
)
models = []
for output in raw_outputs:
    item = {
        "name": _jsonable(output.get("name")),
        "failure": _jsonable(output.get("failure")),
        "molpdf": _jsonable(output.get("molpdf")),
        "DOPE score": _jsonable(output.get("DOPE score")),
        "GA341 score": _jsonable(output.get("GA341 score")),
    }
    if item["name"]:
        item["path"] = str(Path(item["name"]).resolve())
    models.append(item)

ok_models = [item for item in models if item.get("failure") is None and item.get("name")]
if not ok_models:
    raise RuntimeError("MODELLER did not produce any successful models")

if all(item.get("DOPE score") is not None for item in ok_models):
    ok_models.sort(key=lambda item: item["DOPE score"])
    selection_reason = "lowest_dope_score"
else:
    selection_reason = "first_successful_model"

selected = ok_models[0]
Path(config["result_json"]).write_text(json.dumps({
    "all_models": models,
    "successful_models": ok_models,
    "selected_model": selected,
    "selection_reason": selection_reason,
}, indent=2))
'''
    )


def modeller_from_alignment(
    template_pdb: str,
    target_sequence: Optional[str] = None,
    num_models: int = 1,
    template_code: Optional[str] = None,
    target_code: str = "target",
    alignment_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    hetatm: bool = False,
    random_seed: Optional[int] = None,
    target_sequences: Optional[list[str]] = None,
    template_chains: Optional[list[str]] = None,
    loop_refinement: bool = False,
    loop_models: int = 2,
    loop_min_length: int = 1,
    loop_max_length: int = 30,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Build a comparative model with MODELLER and optionally attach it to a source node.

    Supports single-chain monomers and multi-chain complexes (e.g. heterodimers).
    Three input modes, in priority order:

    1. ``alignment_file``: a fully specified MODELLER PIR/ALI alignment. Used as
       is; works for any number of chains (chains separated by ``/``).
    2. ``target_sequences``: one amino-acid sequence per target chain. When two or
       more are given the tool builds the complex alignment automatically with
       MODELLER ``align2d`` against the template structure (chains joined with
       ``/``). Use ``template_chains`` to pick/order the template chains that map
       to ``target_sequences``; when omitted, all template chains are used in file
       order.
    3. ``target_sequence``: a single chain, aligned with MODELLER ``auto_align``.

    Set ``loop_refinement=True`` to fill and refine missing residues with
    MODELLER loop modeling (``LoopModel``): the base comparative model builds the
    full target sequence (including residues absent from the template), then
    every gap loop is rebuilt by the dedicated loop protocol. ``loop_models`` is
    the number of refined loop models per base model; ``loop_min_length`` /
    ``loop_max_length`` bound which gap loops are refined (defaults 1..30 cover
    typical missing-residue stretches). To model the missing residues of a
    structure, pass that structure as the template and its full
    sequence (e.g. from SEQRES) as the target.

    MODELLER is an optional dependency. Users install it separately (for example,
    ``conda install salilab::modeller``) and provide their license via a
    ``KEY_MODELLER*`` environment variable.
    """
    logger.info("Starting MODELLER comparative modeling job")
    job_id = generate_job_id()

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw.research.source_core import (
            _resolve_source_artifacts_dir,
            _validate_source_node,
        )
        from mdclaw._node import begin_node, fail_node

        _node_err = _validate_source_node(job_dir, node_id)
        if _node_err:
            return {
                "success": False,
                "job_id": job_id,
                "output_dir": None,
                "file_path": None,
                "all_models": [],
                "selected_model": None,
                "errors": [_node_err],
                "warnings": [],
            }

    base_dir = _resolve_source_artifacts_dir(job_dir, node_id) if _node_mode else (
        Path(output_dir) if output_dir else WORKING_DIR
    )
    out_dir = create_unique_subdir(base_dir, "modeller")

    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": str(out_dir),
        "file_path": None,
        "all_models": [],
        "selected_model": None,
        "errors": [],
        "warnings": [],
    }

    template_path = Path(template_pdb).expanduser()
    if not template_path.exists():
        result["errors"].append(f"template_pdb does not exist: {template_pdb}")
        return result

    if num_models < 1:
        result["errors"].append("num_models must be >= 1")
        return result

    if loop_refinement and loop_models < 1:
        result["errors"].append("loop_models must be >= 1 when loop_refinement is set")
        result["code"] = "modeller_loop_models_invalid"
        return result

    if target_sequence is not None and target_sequences is not None:
        result["errors"].append(
            "Provide either target_sequence (single chain) or target_sequences "
            "(one per chain), not both"
        )
        result["code"] = "modeller_target_sequence_conflict"
        return result

    # Normalize per-chain target sequences and decide single- vs multi-chain mode.
    chain_sequences = [
        seq.strip() for seq in (target_sequences or []) if seq and seq.strip()
    ]
    if target_sequences is not None and not chain_sequences:
        result["errors"].append(
            "target_sequences must contain at least one non-empty sequence"
        )
        result["code"] = "modeller_target_sequence_required"
        return result
    multichain = len(chain_sequences) > 1

    template_chains_clean = [
        ch.strip() for ch in (template_chains or []) if ch and ch.strip()
    ]
    if template_chains_clean and chain_sequences and not alignment_file:
        if len(template_chains_clean) != len(chain_sequences):
            result["errors"].append(
                "template_chains length "
                f"({len(template_chains_clean)}) must match target_sequences "
                f"length ({len(chain_sequences)})"
            )
            result["code"] = "modeller_chain_count_mismatch"
            return result

    if not alignment_file and not target_sequence and not chain_sequences:
        result["errors"].append(
            "Provide target_sequence, target_sequences, or alignment_file"
        )
        return result

    if not _has_modeller_license_env():
        result["errors"].append(
            "MODELLER license environment variable not found "
            "(expected KEY_MODELLER10v8 or another KEY_MODELLER* variable)"
        )
        result["errors"].append(
            "Install MODELLER separately (for example: conda install salilab::modeller) "
            "and export KEY_MODELLER10v8=<your license key> before running"
        )
        result["code"] = "modeller_license_env_missing"
        return result

    template_code_clean = _sanitize_modeller_code(
        template_code or template_path.stem, "template"
    )
    target_code_clean = _sanitize_modeller_code(target_code or "target", "target")

    template_copy = out_dir / f"{template_code_clean}.pdb"
    shutil.copy2(template_path, template_copy)

    # Resolve the template chain segment used by the multi-chain aligner.
    template_segment = None
    if template_chains_clean:
        template_segment = [
            f"FIRST:{template_chains_clean[0]}",
            f"LAST:{template_chains_clean[-1]}",
        ]

    auto_align = False
    build_alignment_in_runner = False
    if alignment_file:
        # Mode 1: user-supplied alignment, used verbatim (any chain count).
        src_alignment = Path(alignment_file).expanduser()
        if not src_alignment.exists():
            result["errors"].append(f"alignment_file does not exist: {alignment_file}")
            return result
        alignment_text = src_alignment.read_text()
        for code in (template_code_clean, target_code_clean):
            if f">P1;{code}" not in alignment_text:
                result["errors"].append(
                    f"alignment_file does not contain MODELLER entry '>P1;{code}'"
                )
        if result["errors"]:
            return result
        alignment_path = out_dir / src_alignment.name
        shutil.copy2(src_alignment, alignment_path)
    elif multichain:
        # Mode 2: build the complex alignment with align2d inside the runner.
        build_alignment_in_runner = True
        alignment_path = (
            out_dir / f"{target_code_clean}_{template_code_clean}_align2d.ali"
        )
    else:
        # Mode 3: single chain via auto_align on a seed alignment.
        auto_align = True
        single_sequence = target_sequence or (
            chain_sequences[0] if chain_sequences else ""
        )
        alignment_path = out_dir / f"{target_code_clean}_{template_code_clean}_seed.ali"
        _write_modeller_seed_alignment(
            alignment_path,
            target_code=target_code_clean,
            target_sequence=single_sequence,
            template_code=template_code_clean,
        )

    if _node_mode:
        begin_node(job_dir, node_id)

    runner_path = out_dir / "run_modeller.py"
    config_path = out_dir / "modeller_config.json"
    result_json = out_dir / "modeller_result.json"
    _write_modeller_runner(runner_path)
    config = {
        "alignment_file": alignment_path.name,
        "template_code": template_code_clean,
        "target_code": target_code_clean,
        "num_models": num_models,
        "hetatm": hetatm,
        "random_seed": random_seed,
        "auto_align": auto_align,
        "result_json": result_json.name,
        "multichain": build_alignment_in_runner,
        "target_sequences": chain_sequences if build_alignment_in_runner else None,
        "template_segment": template_segment,
        "loop_refinement": loop_refinement,
        "loop_models": loop_models,
        "loop_min_length": loop_min_length,
        "loop_max_length": loop_max_length,
    }
    config_path.write_text(json.dumps(config, indent=2))

    try:
        completed = subprocess.run(
            [sys.executable, runner_path.name, config_path.name],
            cwd=out_dir,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
        )
        if completed.stdout:
            (out_dir / "modeller.stdout").write_text(completed.stdout)
        if completed.stderr:
            (out_dir / "modeller.stderr").write_text(completed.stderr)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        stdout = e.stdout or ""
        # Persist full logs to disk so the agent-facing error can stay short.
        stdout_log = out_dir / "modeller.stdout"
        stderr_log = out_dir / "modeller.stderr"
        if stdout:
            stdout_log.write_text(stdout)
        if stderr:
            stderr_log.write_text(stderr)
        msg = (
            "MODELLER modeling failed. "
            f"stderr tail: {tail_for_agent(stderr, log_path=str(stderr_log))}"
        )
        result["errors"].append(msg)
        result.setdefault("context", {})["log_artifact"] = str(stderr_log)
        if "No module named 'modeller'" in stderr:
            result["code"] = "modeller_not_installed"
            result["errors"].append(
                "Install MODELLER separately with: conda install salilab::modeller"
            )
        else:
            result["code"] = "modeller_execution_failed"
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result
    except Exception as e:
        msg = f"MODELLER modeling failed: {type(e).__name__}: {e}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if not result_json.exists():
        msg = "MODELLER runner did not write modeller_result.json"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    try:
        parsed = json.loads(result_json.read_text())
    except json.JSONDecodeError as e:
        msg = f"Could not parse MODELLER result JSON: {e}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    successful_models = parsed.get("successful_models") or []
    selected_model = parsed.get("selected_model")
    if not successful_models or not selected_model:
        msg = "MODELLER produced no successful models"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    result["all_models"] = successful_models
    result["selected_model"] = {
        **selected_model,
        "selection_reason": parsed.get("selection_reason", "unknown"),
    }

    selected_path = Path(selected_model.get("path") or selected_model.get("name", ""))
    if not selected_path.is_absolute():
        selected_path = (out_dir / selected_path).resolve()
    if not selected_path.exists():
        msg = f"Selected MODELLER model does not exist: {selected_path}"
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if _node_mode:
        try:
            from mdclaw.research.source_core import (
                _complete_source_node,
                _resolve_source_artifacts_dir,
            )

            artifacts_dir = _resolve_source_artifacts_dir(job_dir, node_id)
            primary_dst = artifacts_dir / f"modeller_prediction_{target_code_clean}.pdb"
            shutil.copy2(selected_path, primary_dst)

            digest = hashlib.sha256()
            digest.update(template_copy.read_bytes())
            digest.update(alignment_path.read_bytes())
            digest.update(str(num_models).encode())
            source_digest = digest.hexdigest()[:12]
            extra = {
                "template_pdb": str(template_path),
                "template_code": template_code_clean,
                "target_code": target_code_clean,
                "target_sequence": target_sequence,
                "target_sequences": chain_sequences if chain_sequences else None,
                "template_chains": template_chains_clean or None,
                "multichain": multichain,
                "loop_refinement": loop_refinement,
                "loop_models": loop_models if loop_refinement else None,
                "alignment_file": str(Path(alignment_file).expanduser()) if alignment_file else None,
                "generated_alignment": str(alignment_path),
                "auto_align": auto_align,
                "num_models_requested": num_models,
                "num_successful_models": len(successful_models),
                "modeller_output_dir": str(out_dir),
                "selected_model": result["selected_model"],
                "hetatm": hetatm,
                "random_seed": random_seed,
            }
            _complete_source_node(
                job_dir,
                node_id,
                primary_dst,
                source_type="modeller",
                source_id=f"modeller_{source_digest}",
                file_format="pdb",
                extra_metadata=extra,
            )
            result["file_path"] = str(primary_dst)
        except Exception as e:
            msg = f"Failed to attach MODELLER prediction to source node: {type(e).__name__}: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            fail_node(job_dir, node_id, errors=[msg])
            return result

    result["success"] = True
    logger.info("MODELLER job %s finished successfully", job_id)
    return result

