# Production Restart and Extension

Use this page only when a production run is being extended, retried, or
debugged. Normal first-time production runs can stay in `SKILL.md` and the
solvent-specific guidance page.

## Recommended Extension Path

Create a new `prod` node with `--continue-from`:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from <completed_prod_node_id> \
  --label "+50ns" \
  --conditions '{"simulation_time_ns": 50}'

mdclaw --job-dir <job_dir> --node-id <extension_prod_node_id> run_production \
  --simulation-time-ns 50.0 --platform CUDA
```

`--simulation-time-ns` is the additional time to run in the new node. Each
prod node writes its own `trajectory.dcd`; concatenate through `md-analyze`
when a continuous trajectory is required.

## Restart Resolution

The Python resolver in `mdclaw/_node.py` is authoritative:

- `--continue-from <completed_prod_node_id>` restarts from exactly that prod node. It prefers
  `state.xml` and falls back to `checkpoint.chk` only for same-GPU
  bit-exact replays (committor / sensitivity analyses). If neither artifact
  exists, the run fails instead of silently choosing another ancestor.
- Plain `--parent-node-ids <prod_node_id>` walks prod ancestors and then falls back to
  the eq ancestor. Use it only when the exact restart source does not matter.
- Fresh `eq -> prod` runs restart from the eq state. The eq state is written
  with `final_step=0`, so the requested production time remains the full first
  production length.

`state.xml` is portable across nodes and GPU models — it is the
preferred restart vehicle in every case. `checkpoint.chk` is kept on
disk for bit-identical reproduction (binary OpenMM checkpoints are
platform-specific). Topology artifacts are XML-only: every restart
reads the same `system.xml` + `topology.pdb` + `state.xml` triple from
the topo ancestor.

## Switching Ensembles Across Nodes

`state.xml` is loaded via `XmlSerializer.deserialize` and only
positions / velocities / periodic box are transferred. Barostat
parameters in the saved state are dropped when the new prod context has
no barostat, and a new barostat in the new context starts in its
default state. This means an NPT-equilibrated state can resume directly
into an NVT prod (and vice versa) without rebuilding the topology — just
set `--pressure-bar 0` (NVT) or `--pressure-bar 1.0` (NPT) on the new
node. A short volume re-equilibration is expected when starting NPT
from an NVT state.

## Same-Node Retry

Re-running the same `prod` node can resume and append to existing artifacts,
but this is an advanced retry path. Prefer creating a new extension node for
planned continuation because it leaves a clearer DAG audit trail.

If a failed retry left an invalid or empty `trajectory.dcd`, `run_production`
may discard stale trajectory/energy artifacts and restart those output files
while still loading the restart state. Check `warnings[]` in the tool result.
