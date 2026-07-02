"""Tests for analyze server registration and lightweight analyses."""

import json

import numpy as np
import pytest

from mdclaw.analyze.equilibration import detect_equilibration
from mdclaw.analyze.metrics import (
    analyze_contact_frequency,
    analyze_rmsf,
)


def test_detect_equilibration_direct_writes_artifacts(tmp_path):
    pytest.importorskip("pymbar")
    rng = np.random.default_rng(20260505)
    transient = rng.normal(loc=3.0, scale=0.05, size=40)
    production = rng.normal(loc=0.0, scale=0.05, size=160)
    series = np.concatenate([transient, production])
    series_path = tmp_path / "observable.npy"
    np.save(series_path, series)

    result = detect_equilibration(
        timeseries_file=str(series_path),
        fast=True,
        nskip=5,
        output_name="equilibration_test",
        _out_dir_override=str(tmp_path / "equilibration"),
    )

    assert result["success"] is True
    assert 0 <= result["t0"] < series.size
    assert result["g"] >= 1.0
    assert result["Neff_max"] > 0.0
    assert result["n_samples"] == series.size
    assert result["n_equilibrated_samples"] == series.size - result["t0"]
    assert (tmp_path / "equilibration" / "equilibration_test.json").is_file()
    assert (tmp_path / "equilibration" / "equilibration_test.csv").is_file()


def test_detect_equilibration_direct_selects_2d_npy_column(tmp_path):
    pytest.importorskip("pymbar")
    series = np.linspace(0.0, 1.0, 60)
    data = np.column_stack([np.zeros_like(series), series])
    series_path = tmp_path / "two_column.npy"
    np.save(series_path, data)

    result = detect_equilibration(
        timeseries_file=str(series_path),
        column=1,
        fast=True,
        nskip=4,
        output_name="two_column_equilibration",
        _out_dir_override=str(tmp_path / "equilibration_2d"),
    )

    assert result["success"] is True
    assert result["source_shape"] == [60, 2]
    assert result["column_index"] == 1
    assert result["n_samples"] == 60
    report = json.loads(
        (tmp_path / "equilibration_2d" / "two_column_equilibration.json").read_text()
    )
    assert report["column_index"] == 1


@pytest.fixture
def tiny_mdtraj_inputs(tmp_path, alanine_dipeptide_pdb):
    md = pytest.importorskip("mdtraj")
    pytest.importorskip("matplotlib")

    top = md.load_pdb(alanine_dipeptide_pdb)
    xyz = np.repeat(top.xyz, 4, axis=0)
    xyz[1:, :, 0] += np.linspace(0.01, 0.03, 3)[:, None]
    traj = md.Trajectory(xyz=xyz, topology=top.topology)
    dcd_path = tmp_path / "tiny.dcd"
    pdb_path = tmp_path / "tiny.pdb"
    traj.save_dcd(str(dcd_path))
    top.save_pdb(str(pdb_path))
    return str(dcd_path), str(pdb_path)


def test_analyze_rmsf_direct_writes_artifacts(tmp_path, tiny_mdtraj_inputs):
    dcd_path, pdb_path = tiny_mdtraj_inputs

    result = analyze_rmsf(
        trajectory_file=dcd_path,
        reference_pdb=pdb_path,
        selection="all",
        align_selection="all",
        by_residue=False,
        output_name="rmsf_test",
        _out_dir_override=str(tmp_path / "rmsf"),
    )

    assert result["success"] is True
    assert result["n_frames"] == 4
    assert result["mean_rmsf_nm"] >= 0.0
    assert (tmp_path / "rmsf" / "rmsf_test.npy").is_file()
    assert (tmp_path / "rmsf" / "rmsf_test.csv").is_file()
    assert (tmp_path / "rmsf" / "rmsf_test.png").is_file()


def test_analyze_contact_frequency_direct_writes_artifacts(tmp_path, tiny_mdtraj_inputs):
    dcd_path, pdb_path = tiny_mdtraj_inputs

    result = analyze_contact_frequency(
        trajectory_file=dcd_path,
        reference_pdb=pdb_path,
        selection_group1="all",
        cutoff_nm=0.5,
        mode="residue",
        by_residue=True,
        output_name="contacts_test",
        _out_dir_override=str(tmp_path / "contacts"),
    )

    assert result["success"] is True
    assert result["n_frames"] == 4
    assert result["max_contact_frequency"] >= 0.0
    assert (tmp_path / "contacts" / "contacts_test.npy").is_file()
    assert (tmp_path / "contacts" / "contacts_test.csv").is_file()
    assert (tmp_path / "contacts" / "contacts_test.png").is_file()
