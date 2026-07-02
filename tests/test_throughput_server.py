"""Tests for the throughput estimator used by md-study budget planning."""

from __future__ import annotations

import pytest

from mdclaw.throughput import TOOLS
from mdclaw.throughput.estimate import estimate_md_throughput


def test_in_table_a100_30k_returns_medium_confidence():
    out = estimate_md_throughput(atom_count=30000, gpu_type="A100")
    assert out["success"] is True
    assert out["gpu_type_normalized"] == "a100"
    assert out["confidence"] == "medium"
    assert out["ns_per_day"] == pytest.approx(870.0, rel=0.05)
    assert "source" in out and "ambermd.org" in out["source"]


def test_in_table_h100_30k_returns_medium_confidence():
    out = estimate_md_throughput(atom_count=30000, gpu_type="H100 PCIe")
    assert out["success"] is True
    assert out["gpu_type_normalized"] == "h100"
    assert out["confidence"] == "medium"
    assert out["ns_per_day"] == pytest.approx(1000.0, rel=0.05)


def test_alias_rtx_4090_normalizes():
    out = estimate_md_throughput(atom_count=30000, gpu_type="rtx 4090")
    assert out["success"] is True
    assert out["gpu_type_normalized"] == "rtx_4090"


def test_atom_count_scaling_inverse_powerlaw():
    """Larger systems get fewer ns/day; scaling exponent ~ 0.85."""
    small = estimate_md_throughput(atom_count=30000, gpu_type="A100")["ns_per_day"]
    big = estimate_md_throughput(atom_count=90000, gpu_type="A100")["ns_per_day"]
    assert small > big > 0
    # 30k -> 90k = 3x atom count; expected ratio ~ 3^0.85 ~ 2.55
    ratio = small / big
    assert 2.0 < ratio < 3.2


def test_extrapolation_500k_marks_low_confidence():
    out = estimate_md_throughput(atom_count=500000, gpu_type="A100")
    assert out["success"] is True
    assert out["confidence"] == "low"
    assert any("validated band" in w or "extrapolat" in w for w in out["warnings"])


def test_extrapolation_5k_marks_low_confidence():
    out = estimate_md_throughput(atom_count=5000, gpu_type="A100")
    assert out["success"] is True
    assert out["confidence"] == "low"


def test_unknown_gpu_returns_error_code():
    out = estimate_md_throughput(atom_count=30000, gpu_type="MyMadeUpGPU")
    assert out["success"] is False
    assert out["code"] == "unknown_gpu_type"
    assert out["ns_per_day"] is None


def test_invalid_atom_count_returns_error_code():
    out = estimate_md_throughput(atom_count=0, gpu_type="A100")
    assert out["success"] is False
    assert out["code"] == "invalid_atom_count"


def test_invalid_timestep_returns_error_code():
    out = estimate_md_throughput(atom_count=30000, gpu_type="A100", timestep_fs=-1.0)
    assert out["success"] is False
    assert out["code"] == "timestep_unsupported"


def test_no_hmr_caps_timestep_and_downgrades_confidence():
    out = estimate_md_throughput(
        atom_count=30000, gpu_type="A100", timestep_fs=4.0, hmr=False
    )
    assert out["success"] is True
    assert out["confidence"] == "low"
    assert out["effective_timestep_fs"] == 2.0
    assert out["ns_per_day"] == pytest.approx(435.0, rel=0.05)  # 870 * (2/4)


def test_non_default_chemistry_downgrades_confidence():
    out = estimate_md_throughput(
        atom_count=30000, gpu_type="A100", force_field="ff14SB", water_model="tip3p"
    )
    assert out["success"] is True
    assert out["confidence"] == "low"


def test_cpu_returns_low_throughput():
    out = estimate_md_throughput(atom_count=30000, gpu_type="CPU")
    assert out["success"] is True
    assert out["gpu_type_normalized"] == "cpu"
    assert out["ns_per_day"] < 10.0


def test_apple_metal_alias_m2_max():
    out = estimate_md_throughput(atom_count=30000, gpu_type="M2 Max")
    assert out["success"] is True
    assert out["gpu_type_normalized"] == "apple_metal"


def test_tool_registered_in_tools_dict():
    assert "estimate_md_throughput" in TOOLS
    assert TOOLS["estimate_md_throughput"] is estimate_md_throughput
