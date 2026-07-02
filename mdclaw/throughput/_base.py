"""Throughput Server — coarse OpenMM ns/day estimates for budget-aware planning.

This server exposes a single tool, ``estimate_md_throughput``, that returns
an approximate molecular-dynamics throughput (nanoseconds per simulation
day) for a given system size and GPU type. It is intended to support
study-level budget planning in ``md-study``: the planner asks the user
for a compute budget, calls this tool to convert "GPU type + atom count"
into ns/day, and uses that to propose a feasible (replicates × length)
plan.

The tool is a lookup, not a measurement. The baked-in values are derived
from published AMBER pmemd.cuda benchmarks scaled to approximate OpenMM
performance and anchored at 30,000 atoms with HMR 4 fs:

- AMBER pmemd.cuda benchmark page: https://ambermd.org/GPUPerformance.php
- Exxact AMBER 24 NVIDIA GPU benchmarks (H100 / RTX 6000 Ada):
  https://www.exxactcorp.com/blog/molecular-dynamics/amber-molecular-dynamics-nvidia-gpu-benchmarks
- SaladCloud OpenMM 25-GPU benchmark (used to estimate the AMBER -> OpenMM
  scaling factor ~ 0.85): https://blog.salad.com/openmm-gpu-benchmark/
- Eastman et al. JCTC 19, 5556 (2023). OpenMM 8 reference benchmarks.

The values are approximate. The returned ``confidence`` field is
``"medium"`` inside the atom-count band ``[10000, 300000]`` and ``"low"``
outside it. Non-default ``timestep_fs`` or ``hmr=False`` also downgrade
confidence to ``"low"``.
"""

from __future__ import annotations


from mdclaw._common import setup_logger

logger = setup_logger(__name__)

# Anchor: ns/day at 30,000 atoms, OpenMM 8, ff19SB + OPC, PME 9 A cutoff,
# HMR 4 fs, NPT. Derived from AMBER pmemd.cuda DHFR NPT 4fs benchmarks
# (~23.5k atoms) scaled by 0.85 to approximate OpenMM throughput, then
# rescaled to 30k atoms with the power-law below.
_NS_PER_DAY_AT_30K: dict[str, float] = {
    "h100": 1000.0,
    "rtx_6000_ada": 1200.0,
    "rtx_4090": 1180.0,
    "rtx_4080": 950.0,
    "rtx_a6000": 720.0,
    "a100": 870.0,
    "a100_sxm4": 900.0,
    "rtx_3090": 840.0,
    "v100": 650.0,
    "t4": 320.0,
    "apple_metal": 60.0,
    "cpu": 3.0,
}

# Normalize free-text GPU names from users (e.g. "A100 80GB", "RTX 4090",
# "M2 Max", "no GPU") onto the keys of _NS_PER_DAY_AT_30K. Order matters
# for substring matches; check more specific patterns first.
_GPU_ALIASES: list[tuple[str, str]] = [
    ("rtx 6000 ada", "rtx_6000_ada"),
    ("rtx_6000_ada", "rtx_6000_ada"),
    ("6000 ada", "rtx_6000_ada"),
    ("rtx a6000", "rtx_a6000"),
    ("rtx_a6000", "rtx_a6000"),
    ("a6000", "rtx_a6000"),
    ("a100 sxm", "a100_sxm4"),
    ("a100_sxm4", "a100_sxm4"),
    ("h100 pcie", "h100"),
    ("h100 nvl", "h100"),
    ("h100 sxm", "h100"),
    ("h100", "h100"),
    ("a100", "a100"),
    ("rtx 4090", "rtx_4090"),
    ("rtx_4090", "rtx_4090"),
    ("4090", "rtx_4090"),
    ("rtx 4080", "rtx_4080"),
    ("rtx_4080", "rtx_4080"),
    ("4080", "rtx_4080"),
    ("rtx 3090", "rtx_3090"),
    ("rtx_3090", "rtx_3090"),
    ("3090", "rtx_3090"),
    ("v100", "v100"),
    ("t4", "t4"),
    ("apple", "apple_metal"),
    ("metal", "apple_metal"),
    ("m1 max", "apple_metal"),
    ("m1 pro", "apple_metal"),
    ("m1 ultra", "apple_metal"),
    ("m2 max", "apple_metal"),
    ("m2 pro", "apple_metal"),
    ("m2 ultra", "apple_metal"),
    ("m3 max", "apple_metal"),
    ("m3 pro", "apple_metal"),
    ("m3 ultra", "apple_metal"),
    ("m1", "apple_metal"),
    ("m2", "apple_metal"),
    ("m3", "apple_metal"),
    ("cpu", "cpu"),
    ("none", "cpu"),
    ("no gpu", "cpu"),
]

# Power-law scaling: ns_per_day(N) = base * (30000 / N) ** _SCALING_EXPONENT.
# Fit empirically against AMBER DHFR / FactorIX / Cellulose / STMV points
# (~23k -> ~1M atoms). Exponent ~ 0.85.
_SCALING_EXPONENT = 0.85
_ANCHOR_ATOMS = 30000
_CONFIDENCE_BAND = (10000, 300000)
_OPENMM_FROM_AMBER_FACTOR = 0.85  # documented in module docstring

_SOURCE_CITATION = (
    "Derived from AMBER pmemd.cuda DHFR NPT 4fs benchmarks "
    "(ambermd.org/GPUPerformance.php; Exxact AMBER 24 benchmarks) scaled "
    "by ~0.85 for OpenMM equivalence (per SaladCloud OpenMM benchmark "
    "blog.salad.com/openmm-gpu-benchmark and Eastman et al. JCTC 19, "
    "5556 (2023)). Anchor: 30000 atoms, OpenMM 8, ff19SB+OPC, PME 9 A, "
    "HMR 4 fs, NPT. Power-law scaling with atoms, exponent 0.85."
)

_DEFAULT_ASSUMPTIONS = [
    "OpenMM 8.x with ff19SB + OPC + PME 9 A cutoff",
    "HMR enabled, 4 fs timestep",
    "NPT ensemble",
    "Single GPU; multi-GPU scaling not modeled here",
    "Power-law scaling with atom count, exponent 0.85",
    "Values derived from AMBER pmemd.cuda * 0.85 OpenMM-equivalence factor",
]
