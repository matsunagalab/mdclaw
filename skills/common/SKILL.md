---
name: common
description: "Shared MDClaw conventions used by every md-* skill: the canonical job run loop (inspect -> create -> explain -> run), tool output contract, solvent regime routing, chemistry diagnostics, and handoff rules. Read the referenced page when an MDClaw skill points here; this skill is support material, not a workflow entry point."
---

# MDClaw Common Conventions

This directory holds the shared pages that the MDClaw workflow skills
(`md-prepare`, `md-equilibration`, `md-production`, `md-analyze`, `md-study`,
`md-fep`, `md-abfe`, `md-we`, `hpc-run`, and the model-backend skills)
reference as `skills/common/<page>.md`. It is installed alongside them so
those references resolve; start from the skill that matches the user's
request, not from here.

| Page | Contents |
|---|---|
| `preamble.md` | Role framing and global rules shared by all MDClaw skills. |
| `run-loop.md` | The canonical node run loop: inspect once on entry, then `create -> explain -> run` per stage. The DAG moves artifacts; the agent does not. |
| `tool-output.md` | How to read MDClaw CLI output: envelope, `dag` / `next` blocks, stable guardrail `code`s. |
| `solvent-regimes.md` | Routing between explicit water, implicit solvent, vacuum, and membrane preparation. |
| `implicit-solvent-contract.md` | What the implicit-solvent path supports and refuses. |
| `chemistry-diagnostics.md` | Diagnosing chemistry guardrail failures during preparation. |
| `visual-qa.md` | Structure preview and visual quality-assurance conventions. |
| `md-handoff.md` | Handoff from source-producing prediction skills (Boltz, MODELLER, BioEmu) to MD preparation. |
