# PLUMED runtime and build contract

MDClaw supports a bounded history-free PLUMED input through `run_production`.
The canonical user procedure is [the production leaf](../../skills/md-production/plumed.md).
No changes to the unbiased topo System or new DAG node type are needed.

Continuation rebuilds from the topo System plus original PLUMED input and the
State XML, not `runtime_system.xml`. The latter is an audit snapshot containing
node-specific output paths. Upstream 2.1 also omits ForceGroup serialization;
do not use a deserialized snapshot to infer bias-group energy or to launch a
new run. Bias energies here come from the explicit PLUMED columns.

The raw COLVAR time is PLUMED's absolute step count times the timestep.
Normalized CSV `time_ps` follows the restored OpenMM State time, including any
pre-existing offset between that time and the step counter.

## conda

`environment.yml` includes the source-build tools. After creating/updating and
activating the environment, compile into **that same prefix**:

```bash
conda activate mdclaw
bash container/scripts/build-plumed.sh "$CONDA_PREFIX" /tmp/mdclaw-plumed-build-unique 4
python -m pytest -q tests/test_plumed.py
```

GPU use also requires a driver compatible with the environment's OpenMM/CUDA
build. In validation, conda OpenMM 8.5.1/Python 3.12 selected CUDA 12.9; the
host's CUDA-12.4-capable driver rejected its PTX. CPU tests passed in that
environment, while the source-built CUDA-11.8 container passed CPU and CUDA.
Do not downgrade OpenMM to work around this: use a compatible driver or the
container. Plugin compilation alone does not establish CUDA compatibility.

Use a new build directory; the helper refuses to erase/reuse an existing one.
It pins PLUMED 2.9.4 (`52da2bb76d37dbad0d19f59c6fe0b2ab939e3ded`)
and openmm-plumed 2.1 (`95bfd46d6499625de03ea2151aec42edeae5f662`).
The helper applies one two-line `box-pointer-lifetime-v1` patch: upstream's
`setBox` pointer referred to a block-local array destroyed before calculation.
This silently broke periodic-image invariance in the optimized conda build;
moving the array to the enclosing calculation scope made the three COM/angle/
torsion image-invariance tests pass. The build manifest records this patch.
Do not install a prebuilt plugin that constrains OpenMM below 8.5: that would
break the modern topology stack. The helper installs only its own wheel with
`--no-deps --no-build-isolation`, preserving OpenMM/NumPy. A source-built plugin
must be rebuilt if its OpenMM ABI changes. Activate the intended environment
first; inherited compiler flags pointing to another conda prefix are unsafe.

The helper builds PLUMED without MPI/Python/libtorch support, uses its default
modules, then links the OpenMM plugin against the supplied prefix. Runtime
dependencies must resolve there or to compatible OS libraries. Build identity
is saved in `share/mdclaw/plumed-build.json` and recorded with CV metadata.

## Containers

Both Dockerfiles call the same helper after their source-built OpenMM and
TorchForce. The final runtime copies `/opt/mdclaw`, including PLUMED libraries
and the Python wrapper. `MDCLAW_PLUMED_VERSION` declares this image capability.
Use a new test tag/SIF first; Python overlay alone cannot supply the plugin.
Verify the **final runtime**, not only the builder: import, real CPU forces,
CUDA execution when a GPU is present, restart and CLI stdout JSON. GPU OpenMM
does not imply every PLUMED operation is GPU-accelerated.

## Ownership and restart

`simulation/plumed.py` validates a small action/keyword allowlist, offsets only
MOVINGRESTRAINT STEP values to the protocol origin, and confines PRINT to the
node's COLVAR. Arbitrary PLUMED parsing, external file bundling, history state,
multiwalker methods and bias mixing are deliberately outside this contract.
PLUMED evaluates its schedule per MD step; the Python distance/Torch steering
clock must not update PLUMED in parallel. Initial/final energy queries and
repeated force evaluation at the same step are included in runtime tests.

Continuation reloads the unbiased topo System and adds one PlumedForce plus a
zero-energy force holding a 52-bit protocol digest in an XML Context parameter.
The XML step/time and companion protocol restore progress before first force
evaluation; ordinary XML ensemble-switch behavior remains unchanged. The input
hash/timestep must match. Each child starts fresh files (`setRestart(False)`),
not an append to its parent. This is valid only for the supported history-free
potentials, not metadynamics or cumulative-work checkpointing.

The plugin's C stdout is redirected at the file-descriptor level for the
production lifetime, including Context destruction. No process cwd changes
are needed. Like the CLI, this path assumes one production tool per process;
do not run simultaneous in-process PLUMED productions in threads.
