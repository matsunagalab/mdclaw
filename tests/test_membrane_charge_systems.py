"""Real lipid-only charge controls, without receptor-specific residues."""

import json
from pathlib import Path
import shutil
import pytest

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("water", ["opc", "tip3p"])
def test_mixed_lipid_patch_uses_force_field_charge(tmp_path, water):
    from mdclaw.solvation.membrane import _compute_membrane_net_charge
    import mdclaw

    root = Path(mdclaw.__file__).parent / "data/membrane_patches"
    matches = []
    for path in root.glob("*/*/manifest.json"):
        manifest = json.loads(path.read_text())
        params = manifest.get("parameters", {})
        if params.get("lipids") == "POPC:POPE:CHL1" and params.get("water_model") == water:
            matches.append(path.parent)
    assert len(matches) == 1, matches
    path = matches[0]
    result = _compute_membrane_net_charge(
        pdb_file=path / "patch.pdb",
        box_dims=json.loads((path / "box_dimensions.json").read_text()),
        water_model=water,
    )
    assert result["success"], result
    assert result["net_charge"] == 0


def test_anionic_lipid_charge_is_included(tmp_path):
    from mdclaw.solvation.patch_membrane import _build_patch_packmol_args
    from mdclaw.solvation.membrane import (
        _run_packmol_memgen_noninteractive,
        _compute_membrane_net_charge,
    )

    packed = tmp_path / "packed.pdb"
    args = _build_patch_packmol_args(
        lipids="POPG",
        ratio="1",
        patch_side=30.0,
        dist_wat=17.5,
        leaflet=23.0,
        water_model="opc",
        nloop=20,
        nloop_all=100,
        salt=False,
        salt_c="Na+",
        salt_a="Cl-",
        saltcon=1.0,
        output_file=packed,
        packlog=tmp_path / "packmol",
        packmol_path=shutil.which("packmol"),
    )
    result = _run_packmol_memgen_noninteractive(args, cwd=tmp_path, timeout=180)
    assert packed.is_file(), result
    lines = packed.read_text().splitlines()
    # Packmol writes coordinates without CRYST1; these are the requested
    # packing dimensions, matching the native patch builder's convention.
    box = {"box_a": 30.0, "box_b": 30.0, "box_c": 81.0}
    atoms = [line for line in lines if line.startswith(("ATOM  ", "HETATM"))]
    phosphorus = sum(line[12:16].strip() == "P31" for line in atoms)
    assert phosphorus > 0
    noions = tmp_path / "noions.pdb"
    noions.write_text(
        "\n".join(
            line
            for line in lines
            if not (
                line.startswith(("ATOM  ", "HETATM"))
                and line[17:20].strip().upper() in {"NA", "NA+", "CL", "CL-", "K", "K+"}
            )
        )
        + "\n"
    )
    result = _compute_membrane_net_charge(pdb_file=noions, box_dims=box, water_model="opc")
    assert result["success"], result
    assert result["net_charge"] == -phosphorus


def test_embed_in_membrane_accepts_ligand_chemistry_records_or_a_file(tmp_path):
    """prep registers the records themselves; a path must keep working too."""
    from mdclaw.solvation.membrane import _coerce_ligand_chemistry

    records = [{"name": "LIG", "net_charge": -1}]
    assert _coerce_ligand_chemistry(records) == records
    assert _coerce_ligand_chemistry(records[0]) == records
    path = tmp_path / "ligand_chemistry.json"
    path.write_text(json.dumps(records))
    assert _coerce_ligand_chemistry(str(path)) == records
