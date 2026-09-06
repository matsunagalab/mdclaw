# PLUMED: CV recording, steering and fixed umbrellas

Use `run_production --plumed-file input.dat` for the supported history-free
PLUMED route. Keep the normal study/job/node workflow from SKILL.md. Do not
combine it with `--distance-restraints` or `--custom-force-script`.
For arbitrary Python/ML potentials use [custom-force.md](custom-force.md).

## Environment and supported inputs

The runtime must contain PLUMED and an openmm-plumed build compatible with its
OpenMM, including `setMasses` (plugin >=2.1). MDClaw's images build PLUMED 2.9.4
and plugin 2.1 from pinned sources. For conda installation see
[the build guide](../../docs/developer/plumed.md); installing an incompatible
conda plugin must not downgrade the project's OpenMM.

| Supported | Contract |
|---|---|
| `DISTANCE`, `ANGLE`, `TORSION` | Scalar CVs; explicit `ATOMS`; optional `NOPBC` |
| `GROUP`, `COM`, `CENTER` | Explicit `ATOMS`; COM uses physical elemental masses, not HMR masses |
| `WHOLEMOLECULES` | Explicit `ENTITY0`, `ENTITY1`, … to reconstruct molecules across PBC |
| `RESTRAINT` | Harmonic fixed bias with explicit `ARG`, `AT`, `KAPPA` |
| `MOVINGRESTRAINT` | `STEP0=0`, increasing integer steps; explicit `AT0`, `KAPPA0`; later AT/KAPPA may inherit |
| `PRINT` | Exactly one, `FILE=COLVAR`, explicit CV/bias/center/kappa columns; stride equals MD output interval |
| `UNITS` | Only nm, ps, kJ/mol; angles are radians |

Simple labels, literal atom indices/ranges and native multiline `...` blocks
(closed by a bare `...`) are supported. Macros, regex/wildcards, INCLUDE/LOAD,
external reference files, metadynamics and other history-dependent actions are
not accepted. The runner manages paths and flushing. Do not add RESTART or
change output paths. Work accumulation/Jarzynski analysis is not guaranteed.

## Choose atoms and the starting CV first

PLUMED atom indices start at **1**, OpenMM/mdtraj indices at **0**. Resolve
selections against the actual topo ancestor's PDB; add 1 when writing ATOMS.
Do not use PDB atom serial numbers as array indices. Verify atom counts and
ordering, and measure the starting CV from the **eq XML state**, not the
original topo geometry. `AT0` is explicit: MDClaw does not guess it for you.

For periodic systems, use the same imaging/molecule reconstruction in the
measurement and PLUMED input. COM uses elemental masses; virtual sites have
zero mass. This is not an isotope-specific physical-mass reconstruction.
For `TORSION`, use PLUMED's periodic CV/bias convention and check the path
around -pi/pi. Avoid collinear atoms for angles/dihedrals.

## Independent steering → umbrella branches

Example `angle_X.dat`, after checking that atoms 5, 15, 25 are the intended
three atoms and the initial angle is 2.0 rad (replace these illustrative values):

```text
theta: ANGLE ATOMS=5,15,25
b: MOVINGRESTRAINT ...
 ARG=theta
 STEP0=0 AT0=2.0 KAPPA0=1000
 STEP1=1000 AT1=2.2 KAPPA1=1000
...
PRINT ARG=theta,b.bias,b.theta_cntr STRIDE=250 FILE=COLVAR
```

With 2 fs steps, this ramps over 2 ps (0.002 ns) and logs every 0.5 ps. This
is a **functional smoke**, not an equilibration duration. For a distance use
`DISTANCE` with two atoms and nm targets; for a dihedral use `TORSION` with
four atoms and radian targets. A fixed-only input uses
`b: RESTRAINT ARG=theta AT=2.2 KAPPA=1000` instead of MOVINGRESTRAINT.

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids <common_eq_id> --label steered_X \
  --conditions '{"simulation_time_ns":0.002,"steering_time_ns":0.002}'
mdclaw explain_node --job-dir <job_dir> --node-id <steered_id>
mdclaw --job-dir <job_dir> --node-id <steered_id> run_production \
  --plumed-file angle_X.dat --simulation-time-ns 0.002 --steering-time-ns 0.002 \
  --timestep-fs 2 --output-frequency-ps 0.5

mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from <steered_id> --label umbrella_X \
  --conditions '{"simulation_time_ns":0.002}'
mdclaw explain_node --job-dir <job_dir> --node-id <umbrella_id>
mdclaw --job-dir <job_dir> --node-id <umbrella_id> run_production \
  --simulation-time-ns 0.002 --timestep-fs 2 --output-frequency-ps 0.5
```

For Y, create **another child of the same eq**, using its own target input;
never seed Y from X. Continue each branch's umbrella from its own parent.
Omitting steering duration after completion keeps the same script at its final
restraint, including further extensions. Do not edit the parent's input.

An interrupted ramp resumes in a **new node** with the original total
`--steering-time-ns`; `--simulation-time-ns` is the additional segment length.
Fixed handoff before the ramp ends is refused. Input/timestep changes within
continuation are refused. Preserve the XML, `plumed.json` and original
`plumed.dat`; use XML, not binary checkpoints. Input STEP values are relative
to the first protocol origin, not reset for each segment. PLUMED updates every
MD step: do not supply `--steering-update-interval-ps` for this route.

## Inspect results

`--platform CUDA` keeps OpenMM's regular forces/integration on the GPU. This
plugin evaluates the supported PLUMED CVs/bias on the CPU and exchanges
coordinates/forces every step; CUDA compatibility is not all-GPU PLUMED.
Measure the overhead for the actual system/CVs rather than assuming it is free.

Each node owns `plumed.dat` (original), `plumed.runtime.dat` (resolved steps and
paths), `plumed.json` (protocol), `plumed.COLVAR`, and `plumed.log`. The runner
also writes `collective_variables.csv` and its metadata with units/build info.
PLUMED bias-energy columns are added automatically and summed into
`bias_energy_kj_mol`; CV-only runs have zero bias energy. A child's outputs
never append to its parent's files; existing PLUMED outputs require a new node.

Check actual CV vs applied center, finite energy and the intended periodic
path. Schedule completion is not target attainment or equilibrium.
CSV rows follow the absolute OpenMM report grid. If the final step is not a
PRINT multiple, the last row precedes the endpoint; use `schedule_complete`
and the final XML step to check completion, not that row's center alone.
Steered nodes are initialization; the analysis chain excludes them when collecting
fixed umbrella samples. Choose burn-in and verify sampling/overlap separately.
SLURM must use a PLUMED-capable image: Python source overlay cannot add native
libraries to an older SIF. Record the image/digest and inspect generated jobs.
