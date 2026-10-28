"""Solvation preflight: refusals and warnings decided before any packing runs.

MDDataBench v5 (2026-10, qwen3.6-35b and kimi-k3):

- 13 attempts solvated with the silent OPC default and were refused only at the
  topology (``solvation_topology_water_model_mismatch``), then rebuilt a
  solvation that took up to 1400 s for a membrane: ``water_model_required``.
- 010_membrane_6kux cli_sif r1 embedded a +14 e receptor with ``--no-salt`` in
  19 minutes and the topology refused the charged cell:
  ``solvation_charged_without_ions``.
- Cached membrane embeds took 77-121 s, cold patch builds 1137-1412 s, and every
  killed build had changed a cache-key argument: ``membrane_patch_cache_miss``.
- 079_soluble_1ewf solvated a 135 A protein in a 179 A cubic box (~580k atoms)
  and its topology stalled on the login node: ``box_advisory`` warnings.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mdclaw._node import complete_node, create_node, read_node
from mdclaw.guardrail_codes import GUARDRAIL_CODES
from mdclaw.solvation import embed_in_membrane, solvate_structure
from mdclaw.solvation import preflight
from mdclaw.solvation.patch_membrane import cached_patch_alternatives, probe_patch_cache
from mdclaw.solvation.constants import (
    patch_equilibration_forcefield,
    patch_equilibration_params,
)


def _atom(serial, resname, resnum, x, y=0.0, z=0.0, chain="A", name="CA", element="C"):
    return (
        f"ATOM  {serial:5d} {name:<4s} {resname:>3s} {chain}{resnum:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}\n"
    )


def _protein(path: Path, resnames: list[str]) -> Path:
    lines = [_atom(i + 1, name, i + 1, 3.8 * i, (i % 3) * 1.5, (i % 2) * 1.5)
             for i, name in enumerate(resnames)]
    path.write_text("".join(lines) + "END\n")
    return path


def _rod(path: Path, length: float, step: float = 1.5) -> Path:
    """An elongated solute along x, a few angstrom wide in y and z."""
    lines, serial, x = [], 1, 0.0
    while x <= length:
        for dy, dz in ((0.0, 0.0), (4.0, 0.0), (0.0, 4.0)):
            lines.append(_atom(serial, "ALA", serial, x, dy, dz))
            serial += 1
        x += step
    path.write_text("".join(lines) + "END\n")
    return path


def _job_with_prep(tmp_path: Path, merged_source: Path, *, solv_conditions=None) -> Path:
    job_dir = tmp_path / "job"
    create_node(str(job_dir), "prep")
    merged = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_text(merged_source.read_text())
    complete_node(str(job_dir), "prep_001", artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
    create_node(str(job_dir), "solv", parent_node_ids=["prep_001"], conditions=solv_conditions)
    return job_dir


def _fake_memgen(monkeypatch, calls=None):
    def run(args, cwd=None, timeout=None):
        if calls is not None:
            calls.append(list(args))
        solute = [
            line for line in Path(args[args.index("--pdb") + 1]).read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        ]
        Path(args[args.index("-o") + 1]).write_text(
            "CRYST1   40.000   40.000   40.000  90.00  90.00  90.00 P 1           1\n"
            + "\n".join(solute)
            + "\nHETATM 9999  O   WAT W   1       9.000   0.000   0.000  1.00  0.00           O\n"
            + "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr("mdclaw.solvation._base.packmol_memgen_wrapper.is_available", lambda: True)
    monkeypatch.setattr("mdclaw.solvation._base.packmol_memgen_wrapper.run", run)


def _no_packing(monkeypatch):
    def run(*args, **kwargs):
        raise AssertionError("packmol-memgen must not run after a preflight refusal")

    monkeypatch.setattr("mdclaw.solvation._base.packmol_memgen_wrapper.is_available", lambda: True)
    monkeypatch.setattr("mdclaw.solvation._base.packmol_memgen_wrapper.run", run)


# ---------------------------------------------------------------------------
# water_model_required
# ---------------------------------------------------------------------------


def test_new_codes_are_registered():
    for code in ("water_model_required", "solvation_charged_without_ions",
                 "membrane_patch_cache_miss"):
        assert code in GUARDRAIL_CODES


def test_solvate_without_water_model_is_refused_before_any_work(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["ALA", "GLY"])
    job_dir = _job_with_prep(tmp_path, source)
    _no_packing(monkeypatch)

    result = solvate_structure(job_dir=str(job_dir), node_id="solv_001")

    assert result["code"] == "water_model_required"
    assert result["supported_water_models"][:2] == ["opc", "tip3p"]
    assert "--water-model <opc|tip3p|" in result["next_action"]
    assert "topology step inherits" in result["next_action"]
    node = read_node(str(job_dir), "solv_001")
    assert node["status"] == "pending"
    assert not (job_dir / "nodes" / "solv_001" / "artifacts" / "solvated.pdb").exists()


def test_embed_without_water_model_is_refused_with_patch_hint(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["ALA", "GLY"])
    job_dir = _job_with_prep(tmp_path, source)
    _no_packing(monkeypatch)

    result = embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="POPC")

    assert result["code"] == "water_model_required"
    assert "embed_in_membrane --water-model" in result["next_action"]
    assert any("opc and tip3p only" in hint for hint in result["hints"])
    assert read_node(str(job_dir), "solv_001")["status"] == "pending"


def test_direct_call_without_water_model_is_refused(tmp_path):
    source = _protein(tmp_path / "p.pdb", ["ALA"])
    result = solvate_structure(pdb_file=str(source), output_dir=str(tmp_path / "out"))
    assert result["code"] == "water_model_required"
    assert result["success"] is False


def test_declared_water_condition_is_inherited(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["ALA", "GLY"])
    job_dir = _job_with_prep(tmp_path, source, solv_conditions={"water_model": "TIP3P"})
    calls: list = []
    _fake_memgen(monkeypatch, calls)

    result = solvate_structure(job_dir=str(job_dir), node_id="solv_001")

    assert result["success"] is True, result.get("errors")
    assert result["parameters"]["water_model"] == "tip3p"
    assert result["parameters"]["water_model_source"] == "node_conditions"
    assert calls[0][calls[0].index("--ffwat") + 1] == "tip3p"
    node = read_node(str(job_dir), "solv_001")
    assert node["metadata"]["water_model"] == "tip3p"
    assert node["metadata"]["water_model_source"] == "node_conditions"


# ---------------------------------------------------------------------------
# --no-salt on a charged solute
# ---------------------------------------------------------------------------


def test_packmol_charge_table_matches_packmol_memgen():
    utils = pytest.importorskip("packmol_memgen.lib.utils")
    assert preflight.PACKMOL_MEMGEN_RESIDUE_CHARGES == utils.charged


def test_solute_charge_estimate_counts_residues_caps_and_ligands(tmp_path):
    charged = _protein(tmp_path / "charged.pdb", ["LYS", "ARG", "LYS", "ASP", "ALA"])
    assert preflight.estimate_solute_charge(charged)["charge"] == 2

    # ACE caps the N terminus; the free C terminus (-1) balances LYS (+1).
    capped = _protein(tmp_path / "capped.pdb", ["ACE", "LYS", "ALA"])
    assert preflight.estimate_solute_charge(capped)["charge"] == 0

    neutral = _protein(tmp_path / "neutral.pdb", ["LYS", "GLU", "ALA"])
    ligand = [{"residue_name": "LIG", "net_charge": -1}]
    assert preflight.estimate_solute_charge(neutral, ligand)["charge"] == -1

    unknown = [{"residue_name": "LIG"}]
    assert preflight.estimate_solute_charge(neutral, unknown)["success"] is False


def test_no_salt_on_charged_solute_is_refused_in_node_mode(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["LYS", "ARG", "LYS", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    _no_packing(monkeypatch)

    result = solvate_structure(job_dir=str(job_dir), node_id="solv_001",
                               water_model="opc", salt=False)

    assert result["code"] == "solvation_charged_without_ions"
    assert result["solute_net_charge_e"] == 3
    assert "--salt --saltcon 0 (counter-ions only)" in result["next_action"]
    assert read_node(str(job_dir), "solv_001")["status"] == "pending"


def test_no_salt_on_charged_solute_warns_outside_node_mode(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["LYS", "ALA"])
    calls: list = []
    _fake_memgen(monkeypatch, calls)

    result = solvate_structure(pdb_file=str(source), output_dir=str(tmp_path / "out"),
                               water_model="opc", salt=False)

    assert result["success"] is True
    assert "--nocounter" in calls[0]
    assert any("places no ions" in w for w in result["warnings"])


def test_no_salt_on_neutral_solute_runs(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    _fake_memgen(monkeypatch)

    result = solvate_structure(job_dir=str(job_dir), node_id="solv_001",
                               water_model="opc", salt=False)

    assert result["success"] is True, result.get("errors")
    assert result["solute_charge_estimate"]["charge"] == 0


def test_embed_no_salt_on_charged_solute_is_refused_before_orientation(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["LYS", "ARG", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    _no_packing(monkeypatch)
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._orient_for_membrane",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("oriented")),
    )

    result = embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="POPC",
                               water_model="opc", salt=False)

    assert result["code"] == "solvation_charged_without_ions"
    assert "embed_in_membrane ... --salt --saltcon 0" in result["next_action"]
    assert read_node(str(job_dir), "solv_001")["status"] == "pending"


# ---------------------------------------------------------------------------
# Membrane patch cache
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_patch_cache(tmp_path, monkeypatch):
    """Bundled patches only: a user cache entry must not turn a miss into a hit."""
    monkeypatch.setenv("MDCLAW_MEMBRANE_CACHE_DIR", str(tmp_path / "patch-cache"))
    monkeypatch.delenv("MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR", raising=False)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setattr("mdclaw.solvation.membrane._packmol_memgen_version", lambda: "test")


def _probe(**overrides):
    params = dict(lipids="POPC", ratio="1", water_model="opc", salt=True, salt_c="Na+",
                  salt_a="Cl-", saltcon=0.15, dist_wat=17.5, leaflet=23.0, patch_side=40.0,
                  nloop=20, nloop_all=100)
    params.update(overrides)
    water = params["water_model"]
    return probe_patch_cache(
        **params,
        equil_params={**patch_equilibration_params(water), "water_model": water},
        forcefield=patch_equilibration_forcefield(water),
    )


def test_bundled_patch_is_a_hit_at_the_defaults(isolated_patch_cache):
    assert _probe()["hit"] is True
    assert _probe(water_model="tip3p")["hit"] is True


def test_cached_alternatives_name_the_changed_argument(isolated_patch_cache):
    probe = _probe(dist_wat=10.0)
    assert probe["hit"] is False

    alternatives = cached_patch_alternatives(
        probe["parameters"], forcefield_for_water=patch_equilibration_forcefield,
    )

    best = alternatives[0]
    assert best["same_composition"] is True
    assert best["differences"] == {"dist_wat": 17.5}
    assert best["arguments"] == "--dist-wat 17.5"

    no_salt = cached_patch_alternatives(
        _probe(salt=False)["parameters"], forcefield_for_water=patch_equilibration_forcefield,
    )[0]
    assert no_salt["arguments"] == "--salt"


def _stub_patch_tiles(monkeypatch, seen):
    def stub(**kwargs):
        seen.update(kwargs)
        return {"success": False, "code": "stopped", "errors": ["stopped"], "warnings": []}

    monkeypatch.setattr("mdclaw.solvation.membrane.embed_with_membrane_patch_tiles", stub)


def test_cache_miss_is_refused_in_node_mode_outside_slurm(tmp_path, monkeypatch, isolated_patch_cache):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    seen: dict = {}
    _stub_patch_tiles(monkeypatch, seen)

    result = embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="POPC",
                               water_model="opc", dist_wat=10.0, preoriented=True)

    assert result["code"] == "membrane_patch_cache_miss"
    assert "--dist-wat 17.5" in result["next_action"]
    assert "--allow-patch-build" in result["next_action"]
    assert "1137-1412 s" in result["message"]
    assert result["membrane_patch_cache"]["cached_alternatives"][0]["differences"] == {"dist_wat": 17.5}
    assert not seen
    assert read_node(str(job_dir), "solv_001")["status"] == "pending"


def test_cache_miss_for_an_uncached_composition_lists_compositions(
    tmp_path, monkeypatch, isolated_patch_cache,
):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    _stub_patch_tiles(monkeypatch, {})

    result = embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="DOPE:DOPG",
                               ratio="3:1", water_model="opc", preoriented=True)

    assert result["code"] == "membrane_patch_cache_miss"
    assert "POPC 1" in result["next_action"]
    assert "--allow-patch-build" in result["next_action"]


@pytest.mark.parametrize("opt_in", ["flag", "slurm"])
def test_cache_miss_builds_with_the_flag_or_inside_slurm(
    tmp_path, monkeypatch, isolated_patch_cache, opt_in,
):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    seen: dict = {}
    _stub_patch_tiles(monkeypatch, seen)
    if opt_in == "slurm":
        monkeypatch.setenv("SLURM_JOB_ID", "12345")

    result = embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="POPC",
                               water_model="opc", dist_wat=10.0, preoriented=True,
                               allow_patch_build=(opt_in == "flag"))

    assert seen, result
    assert result["code"] == "stopped"
    assert any("--dist-wat 17.5" in w for w in result["warnings"])


def test_cache_hit_is_not_refused(tmp_path, monkeypatch, isolated_patch_cache):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    job_dir = _job_with_prep(tmp_path, source)
    seen: dict = {}
    _stub_patch_tiles(monkeypatch, seen)

    embed_in_membrane(job_dir=str(job_dir), node_id="solv_001", lipids="POPC",
                      water_model="tip3p", dist=20.0, preoriented=True)

    assert seen  # --dist is not part of the cache key


def test_read_only_miss_is_refused_before_orientation(tmp_path, monkeypatch, isolated_patch_cache):
    source = _protein(tmp_path / "p.pdb", ["LYS", "GLU", "ALA"])
    seen: dict = {}
    _stub_patch_tiles(monkeypatch, seen)
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._orient_for_membrane",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("oriented")),
    )

    result = embed_in_membrane(pdb_file=str(source), output_dir=str(tmp_path / "out"),
                               lipids="POPC", water_model="opc", leaflet=25.0,
                               membrane_cache_mode="read-only")

    assert result["code"] == "membrane_patch_cache_miss"
    assert "--leaflet 23.0" in result["next_action"]
    assert "--membrane-cache-mode auto" in result["next_action"]
    assert not seen


# ---------------------------------------------------------------------------
# Box size
# ---------------------------------------------------------------------------


def test_box_advisory_reproduces_the_1ewf_boxes():
    """packmol-memgen's box rule on 079_soluble_1ewf's own merged.pdb."""
    merged = Path(
        "/data1/rkp00079/rku00161/runs/kimi-k3-3cond-full-v5/attempts/079_soluble_1ewf/"
        "cli_sif__pi__rikyu-kimi-k3__r1/workspace/study/jobs/main/nodes/solv_001/artifacts/merged.pdb"
    )
    if not merged.exists():
        pytest.skip("campaign evidence not on this host")
    advisory = preflight.box_size_advisory(merged, dist=15.0, cubic=True, water_model="tip3p")
    assert advisory["cubic_box_angstrom"][0] == pytest.approx(179.12, abs=0.2)
    assert advisory["rectangular_box_angstrom"][2] == pytest.approx(89.32, abs=0.2)
    assert advisory["estimated_atoms_cubic"] == pytest.approx(580541, rel=0.03)
    assert advisory["estimated_atoms_rectangular"] == pytest.approx(292460, rel=0.03)
    assert preflight.elongated_cubic_warning(advisory) is not None


def test_elongated_solute_in_cubic_box_warns_with_rectangular_option(tmp_path, monkeypatch):
    rod = _rod(tmp_path / "rod.pdb", length=120.0)
    _fake_memgen(monkeypatch)

    result = solvate_structure(pdb_file=str(rod), output_dir=str(tmp_path / "out"),
                               water_model="opc", dist=15.0)

    advisory = result["box_advisory"]
    assert advisory["principal_extent_ratio"] > 5
    assert advisory["estimated_atoms_rectangular"] < 0.7 * advisory["estimated_atoms_cubic"]
    warning = next(w for w in result["warnings"] if "elongated" in w)
    assert "--no-cubic" in warning
    assert f"~{advisory['estimated_atoms_cubic']:,} atoms" in warning


def test_globular_or_rectangular_request_does_not_warn(tmp_path, monkeypatch):
    rod = _rod(tmp_path / "rod.pdb", length=120.0)
    _fake_memgen(monkeypatch)
    result = solvate_structure(pdb_file=str(rod), output_dir=str(tmp_path / "out"),
                               water_model="opc", cubic=False)
    assert not any("elongated" in w for w in result["warnings"])

    blob = tmp_path / "blob.pdb"
    lines, serial = [], 1
    for i in range(6):
        for j in range(6):
            for k in range(6):
                lines.append(_atom(serial, "ALA", serial, 6.0 * i, 6.0 * j, 6.0 * k))
                serial += 1
    blob.write_text("".join(lines) + "END\n")
    advisory = preflight.box_size_advisory(blob, dist=15.0, cubic=True, water_model="opc")
    assert preflight.elongated_cubic_warning(advisory) is None


def test_large_system_warns_to_submit_topology_and_minimization(tmp_path, monkeypatch):
    source = _protein(tmp_path / "p.pdb", ["ALA", "GLY"])
    job_dir = _job_with_prep(tmp_path, source)
    _fake_memgen(monkeypatch)
    monkeypatch.setattr("mdclaw.solvation._base.count_atoms_in_pdb", lambda path: 620_000)

    result = solvate_structure(job_dir=str(job_dir), node_id="solv_001", water_model="opc")

    assert result["success"] is True, result.get("errors")
    warning = next(w for w in result["warnings"] if w.startswith("Large system"))
    assert "620,000 atoms" in warning
    assert f"mdclaw submit_job --job-dir {job_dir}" in warning
    assert "run_minimization" in warning
    assert warning in read_node(str(job_dir), "solv_001")["warnings"]


def test_large_system_threshold():
    assert preflight.large_system_warning(499_999, job_dir="j") is None
    assert preflight.large_system_warning(500_000, job_dir="j") is not None
