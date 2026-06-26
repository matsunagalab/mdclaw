# Explicit Water: Solvation & Topology

## Decision Defaults

Quick reference only; Python tool signatures and guardrails are authoritative.

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 Å | "buffer 20", "20A" |
| Salt concentration | 0.15 M NaCl | "0.3M", "no salt" |
| Cubic box | true | "octahedral", "truncated octahedron" |
| Force field | ff19SB | "ff14SB" |

**Standard explicit-water pair**: default to `ff19SB + opc`. This pair is
the Amber Manual 2024 recommendation — ff19SB was parameterized against
OPC and behaves incorrectly with TIP3P (guardrail rejects this
combination). Use `ff14SB + tip3p` only to reproduce pre-2019 results.

Prepare-time details (source acquisition, inspection, chain/ligand
selection, metals, PTMs, mutations, and confirmation policy) live in
`setup.md` and apply identically for explicit and implicit solvent.

Supported crystallographic ions are part of the explicit-solvent default: keep
ions requested by the user or detected as supported source ions during
`prepare_complex`, then solvate with the requested/default salt concentration.
Do not call `parameterize_metal_ion` for common supported monatomic ions such
as CA, MG, NA, K, or CL unless a structured tool result reports missing or
coordination-specific metal parameters.
Do not relabel an ion-containing non-periodic topology as implicit; explicit
ions require either the explicit-solvent path or a deliberate vacuum/no-solvent
topology.

---

## Preparation Prerequisite

Complete `setup.md` through `prepare_complex` first. Continue here only after
a completed `prep` node exists. If inspection found multivalent metals or
PTMs, finish the corresponding branched prep steps in `setup.md` before
solvation.

Before solvation, verify that any source ions intentionally kept by the request
are present in the prep `merged_pdb`. If the user requested implicit solvent,
use `implicit-water.md` instead and do not retain explicit ions.

---

## Step 4: Solvation

### Bulk Water

```bash
mdclaw create_node --job-dir <job_dir> --node-type solv
mdclaw --job-dir <job_dir> --node-id <solv_node_id> solvate_structure \
  --dist 15.0 --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` parent's `merged_pdb` artifact.
To override, pass `--pdb-file` explicitly.

Before topology, do a quick request-match check: for ligand-free systems,
the prep node must not carry `ligand_chemistry`, and the `solvated_pdb` must not
contain source ligands. If either check fails, branch from the valid ancestor;
do not rerun the same node with changed inputs. Prefer node artifacts and PDB
contents over stale prose fields in logs or metadata.

After solvation, run a local feasibility preflight before topology/min/eq/prod if
the next stages will run on this machine:

```bash
mdclaw inspect_openmm_platforms \
  --atom-count <result.statistics.total_atoms> \
  --solvent-type explicit
```

If `local_feasibility` is `not_recommended` or `slow_on_cpu`, do not silently
continue into local topology/equilibration/production. Tell the user whether a
CUDA/OpenCL platform was detected and prefer `/hpc-run`, or explicitly switch
to a shorter smoke-test protocol. Reducing the water box is a debugging choice
that changes the system and should be stated as such.

### Membrane

Use the same `solv` node type for membrane embedding:

```bash
mdclaw create_node --job-dir <job_dir> --node-type solv
mdclaw --job-dir <job_dir> --node-id <solv_node_id> embed_in_membrane \
  --lipids POPC --ratio "1" --dist 15.0 --dist-wat 17.5 \
  --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` parent's `merged_pdb` artifact.
Pass `--pdb-file` only to override (e.g., to use a manually oriented PDB).
On success, the solv node records `is_membrane=true` for downstream topology,
equilibration, and production.
Membrane embedding with packmol-memgen is a long-running operation. For large
or mixed membrane systems, tens of minutes are normal, and CPU runs may take
more than an hour. Do not assume a running membrane `solv` node is hung only
because it has been running for 10-30 minutes. Continue monitoring or explain
the same node until it completes, fails, or reaches the configured timeout; do
not create a sibling `solv` node just to retry an in-progress membrane build.
Run membrane embedding in the foreground for autonomous benchmark-style tasks.
Do not exit or package a final submission while the membrane `solv` node is
still `running`; wait until it reaches `completed` or `failed`, then continue
from that concrete node outcome. If you need a simple blocking check after a
long-running membrane command, use:

```bash
mdclaw wait_node --job-dir <job_dir> --node-id <solv_node_id> \
  --timeout-seconds 7200 --poll-interval-seconds 30
```

Membrane embedding runs MDClaw's bounded Packmol retry plan as a 4-lane
parallel race by default (`--packmol-race-lanes 4`). Use
`--packmol-race-lanes 1` only on CPU-constrained/shared hosts when preserving
the previous sequential behavior matters more than wall time.
Explicit solvation and membrane tools first try the requested `--saltcon`
(default 0.15 M). If neutralization requires a higher ion concentration, MDClaw
automatically reruns packmol-memgen with `--salt_override` without changing the
explicit-solvent mode, and records a warning plus metadata for provenance.
If `embed_in_membrane` returns `code="packmol_packing_quality_failed"` and
`recommended_next_action="retry_membrane_with_larger_box"`, keep the requested
lipid species and ratio fixed. The CLI has already retried Packmol with a
bounded adaptive packing budget before returning this failure. Retry from the
same `prep` parent only when the `retry_suggestion.suggested_parameters`
change the lateral xy box via `dist`; keep `leaflet` and `dist_wat` unchanged
unless the user or prompt explicitly asks for a thicker membrane/water slab.
Do not manually increase Packmol loop counts after this failure unless you are
running a deliberate debugging experiment outside the benchmark path. More
generally, membrane retries may adjust packing controls, random seed, or
recommended lateral box/buffer expansion, but must preserve the requested
lipid species, ratios, solute identity, solvent regime, and force-field intent.
If the requested target appears infeasible, stop and report that instead of
silently simplifying the system.
Packmol may write both a postprocessed primary PDB and a raw `*_FORCED` PDB
when it cannot find a perfect packing. MDClaw may continue with the
postprocessed primary PDB as a topology/minimization candidate, but records
`packing_quality.passed=false`. Do not treat the raw `*_FORCED` PDB as the
solvated topology input, because it can bypass packmol-memgen's final
AMBER/LIPID residue-name postprocessing. Trust downstream objective checks
(topology load, finite energy, minimization report) before calling the
preparation usable.

Common structured outcomes:

| `code` / action | What it means | Agent response |
|---|---|---|
| `packmol_imperfect_primary_output_candidate` | Packmol did not reach perfect packing after MDClaw's bounded retry, but packmol-memgen wrote a postprocessed primary PDB. | Continue to `build_amber_system` and `run_minimization`; only trust the candidate if topology load, finite energy, and minimization checks pass. |
| `packmol_packing_quality_failed` + `retry_membrane_with_larger_box` | Packmol could not produce a perfect packing after MDClaw's bounded adaptive retry. The box/packing is not MD-ready. | Retry only with the CLI-provided larger xy/lateral box suggestion unless geometry was explicitly fixed. |
| `forced_output_available` metadata | Packmol wrote a `*_FORCED` PDB during a failed attempt. | Keep it for debugging/provenance only; do not pass it to topology generation. |
| `salt_override_required` metadata | Neutralization needs more ions than the requested salt concentration. | Accept the automatic `--salt_override` rerun and record the warning/provenance. |

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Build Topology

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo
mdclaw --job-dir <job_dir> --node-id <topo_node_id> build_amber_system \
  --no-is-membrane
```

`pdb_file` is auto-resolved from the `solv` parent's `solvated_pdb` artifact.
For membrane systems created by `embed_in_membrane`, pass `--is-membrane`
instead of `--no-is-membrane`.
These are boolean optional CLI flags; do not pass `true` / `false` values.
Do not pass a manual `--pdb-file`; if the wrong structure would be resolved,
fix the upstream `prep`/`solv` branch and create a new `topo` node.

If topology output is quiet, inspect `nodes/<topo_id>/node.json` and report
`metadata.topology_build_stage`, `metadata.topology_build_stage_updated_at`,
and `metadata.topology_build_stage_history`. Long OPC/explicit-water stages
such as `modeller_prepare`, `system_generator_create_system`, and
`initial_minimization` can be slow on CPU; retry or branch only after the node
has failed, completed, or the user explicitly abandons it.

`build_amber_system` is the curated Amber → OpenMM system builder for completed
prep/solv DAG artifacts. It runs the resolved prepared/solvated PDB through
OpenFF Pablo, applies the resolved Amber XML
bundle via `openmmforcefields.SystemGenerator` (`GAFFTemplateGenerator` from
prep's `ligand_chemistry` artifact; NAGL charges are assigned internally), and
emits the modern artifact triple
`system.system.xml` +
`system.topology.pdb` + `system.state.xml` on the topo node, with
`metadata.system_artifact_kind="openmm_system_xml"` and a
`metadata.forcefield_provenance` dict (XML names, sha256, OpenMM /
openmmforcefields versions, `method.hmr`, ligand template sources). HMR defaults
to `--hmr` (4 amu hydrogens) so the run-side default 4 fs timestep is
loadable; the run-time validator rejects mismatched HMR with
`modern_system_hmr_mismatch`. The XML triple is the only topology
contract on the run side — tleap / `parm7` / `rst7` are not produced
or consumed anywhere. The topo node's `state.xml` carries the topology-time
minimized coordinates; `topology.pdb` supplies atom/residue topology. When a PDB
view of the minimized state is needed for reports or MDPrepBench, run:

```bash
mdclaw export_state_pdb \
  --topology-pdb-file <topology.pdb> \
  --state-xml-file <state.xml> \
  --output-pdb-file minimized_structure.pdb
```

To explore an older protein force field that is
not the recommended default, override both sides together — e.g.
`build_amber_system --forcefield ff14SB --water-model tip3p` selects the
ff14SB bundle and TIP3P water in the SystemGenerator XML list, and is a
research / comparison choice, not a "legacy artifact format" toggle.

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable

---

## State Tracking

Each tool auto-updates its `node.json` and job-level `progress.json`.
No manual writing needed. Read `progress.json` to verify state before handoff.
Use `progress.json.params.execution_mode` as the source of truth for
interaction policy.

## Handoff

1. Read `progress.json` -- verify the topology node returned by `create_node`
   has status `completed`.
2. Tell the user:
   ```
   Preparation complete. Next:
     Continue with skills/md-equilibration/SKILL.md on this job_dir.
     Shortcut, if available: /md-equilibration <job_dir>
   ```
   Preparation does not auto-invoke equilibration — each stage is
   user-initiated.
