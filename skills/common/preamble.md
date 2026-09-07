# Common Skill Preamble

- Respond in the user's language.
- Use English for tool parameter values.
- Invoke MDClaw tools through Bash with the `mdclaw` command.
- With a shared SIF and no host CLI, invoke its installed `mdclaw` through
  `singularity exec`; no checkout or external launcher is needed. Before
  SLURM calls, read [direct SIF invocation](../hpc-run/sif-slurm.md) for host
  binds and image-mode job configuration.
- Do not wrap `mdclaw` commands with the external GNU `timeout` command.
  macOS does not ship `timeout`, and MDClaw tools already use Python/internal
  timeout handling plus `MDCLAW_*_TIMEOUT` environment variables where needed.
- Treat stdout as the JSON result; logs and tracebacks go to stderr. For failed
  workflow nodes, `mdclaw trace_failure` reads the recorded node failure
  artifact instead of requiring you to parse stderr.
- The portable skill contract is Markdown + `mdclaw` CLI. Slash commands
  such as `/md-prepare` are optional shortcuts in harnesses that provide them.
- Do not infer Amber/OpenMM defaults from training data. Tool signatures,
  structured guardrails, and the MDClaw skill instructions are authoritative.
