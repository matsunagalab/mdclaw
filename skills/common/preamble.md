# Common Skill Preamble

- Respond in the user's language.
- Use English for tool parameter values.
- Invoke MDClaw tools through Bash with the `mdclaw` command.
- Treat stdout as the JSON result; logs and tracebacks go to stderr.
- Do not infer Amber/OpenMM defaults from training data. Tool signatures,
  structured guardrails, and these runbooks are authoritative.
