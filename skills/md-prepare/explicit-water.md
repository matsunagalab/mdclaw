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

---

## Preparation Prerequisite

Complete `setup.md` through `prepare_complex` first. Continue here only after
a completed `prep` node exists. If inspection found multivalent metals or
PTMs, finish the corresponding branched prep steps in `setup.md` before
solvation.

---

## Step 4: Solvation

### Bulk Water

```bash
mdclaw create_node --job-dir job_xxx --node-type solv --parent-node-ids prep_001
mdclaw --job-dir job_xxx --node-id solv_001 solvate_structure \
  --dist 15.0 --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` parent's `merged_pdb` artifact.
To override, pass `--pdb-file` explicitly.

After solvation, run a local feasibility preflight before topology/eq/prod if
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
mdclaw create_node --job-dir job_xxx --node-type solv --parent-node-ids prep_001
mdclaw --job-dir job_xxx --node-id solv_001 embed_in_membrane \
  --lipids POPC --ratio 1 --dist 15.0 --dist-wat 17.5 \
  --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` parent's `merged_pdb` artifact.
Pass `--pdb-file` only to override (e.g., to use a manually oriented PDB).
On success, the solv node records `is_membrane=true` for downstream topology,
equilibration, and production.

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Build Topology

```bash
mdclaw create_node --job-dir job_xxx --node-type topo --parent-node-ids solv_001
mdclaw --job-dir job_xxx --node-id topo_001 build_amber_system \
  --no-is-membrane
```

`pdb_file` is auto-resolved from the `solv` parent's `solvated_pdb` artifact.
For membrane systems created by `embed_in_membrane`, pass `--is-membrane`
instead of `--no-is-membrane`.

`build_amber_system` is the curated Amber → OpenMM system builder. It runs
the prepared PDB through OpenFF Pablo, applies the resolved Amber XML
bundle via `openmmforcefields.SystemGenerator` (+ `GAFFTemplateGenerator`
for ligands), and emits the modern artifact triple `system.system.xml` +
`system.topology.pdb` + `system.state.xml` on the topo node, with
`metadata.system_artifact_kind="openmm_system_xml"` and a
`metadata.forcefield_provenance` dict (XML names, sha256, OpenMM /
openmmforcefields versions, `method.hmr`, ligand Molecules). HMR defaults
to `--hmr` (4 amu hydrogens) so the run-side default 4 fs timestep is
loadable; the run-time validator rejects mismatched HMR with
`modern_system_hmr_mismatch`. The XML triple is the only topology
contract on the run side — tleap / `parm7` / `rst7` are not produced
or consumed anywhere. To explore an older protein force field that is
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

1. Read `progress.json` -- verify `topo_001` status is `completed`.
2. Tell the user:
   ```
   Preparation complete. Next:
     /md-equilibration job_xxx
   ```
   `/md-prepare` does not auto-invoke equilibration — each stage is
   user-initiated.
