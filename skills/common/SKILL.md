---
name: common
description: "Display existing molecular structures or MD trajectories, and troubleshoot remote Molstar/MCP display: route prepared full-system views, playback and interactive rotation to Molstar when available, and still images to PyMOL. Also supplies shared MDClaw job-loop, output, solvent, chemistry, and handoff conventions for md-* skills."
---

# MDClaw Common Conventions

For a molecule/trajectory display request (including 「再生して」「回して見たい」
「準備後の全系を見せて」 and 「静止画を見せて」), follow [visual-qa.md](visual-qa.md) directly and stop
after display. It owns intent selection, current-session capability checks,
and fallback. Viewing existing files does not start an MD workflow.
Stage-specific contents and any display-copy processing are selected there too.
For remote display failures or a missing Molstar tool, read its **How the MCP
viewer works** and **When Claude cannot find or display the MCP viewer** sections
before choosing a workaround. The execution host and the user's display client
are different roles; MCP connection success is not rendering success.

This directory holds the shared pages that the MDClaw workflow skills
(`md-prepare`, `md-equilibration`, `md-production`, `md-analyze`, `md-study`,
`md-fep`, `md-abfe`, `md-we`, `hpc-run`, and the model-backend skills)
reference as `skills/common/<page>.md`. It is installed alongside them so
those references resolve. For work other than display, start from the workflow
skill that matches the user's request.

| Page | Contents |
|---|---|
| `preamble.md` | Role framing and global rules shared by all MDClaw skills. |
| `run-loop.md` | The canonical node run loop: inspect once on entry, then `create -> explain -> run` per stage. The DAG moves artifacts; the agent does not. |
| `tool-output.md` | How to read MDClaw CLI output: envelope, `dag` / `next` blocks, stable guardrail `code`s. |
| `solvent-regimes.md` | Routing between explicit water, implicit solvent, vacuum, and membrane preparation. |
| `implicit-solvent-contract.md` | What the implicit-solvent path supports and refuses. |
| `chemistry-diagnostics.md` | Diagnosing chemistry guardrail failures during preparation. |
| `visual-qa.md` | Molstar/PyMOL selection by user intent and available display capabilities; optional visual review. |
| `trajectory-display.md` | Host-side preparation snapshots and trajectory display copies: retain lipids/ligands, subsample long runs, correct PBC where supported, and fit to the average only on request. |
| `md-handoff.md` | Handoff from source-producing prediction skills (Boltz, MODELLER, BioEmu) to MD preparation. |
