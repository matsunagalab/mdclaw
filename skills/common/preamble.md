# Common Skill Preamble

- Respond in the user's language.
- Use English for tool parameter values.
- Invoke MDClaw tools through Bash with the `mdclaw` command.
- Do not wrap `mdclaw` commands with the external GNU `timeout` command.
  macOS does not ship `timeout`, and MDClaw tools already use Python/internal
  timeout handling plus `MDCLAW_*_TIMEOUT` environment variables where needed.
- Treat stdout as the JSON result; logs and tracebacks go to stderr.
- The portable skill contract is Markdown + `mdclaw` CLI. Slash commands
  such as `/md-prepare` are optional shortcuts in harnesses that provide them.
- Do not infer Amber/OpenMM defaults from training data. Tool signatures,
  structured guardrails, and these runbooks are authoritative.
