# Skill Authoring Conventions

MDClaw skills are procedure documents that a coding agent reads and follows to
run MD tools through the `mdclaw` CLI. Agents range from strong frontier models
to smaller/weaker models. These conventions keep every skill readable
top-to-bottom by a weak LLM. Apply them to every `skills/**/*.md` file.

The skill tree lives under `skills/`; `.agents/skills/`, `.claude/skills/`, and
`.codex/skills/` are discovery mirrors (symlinks or copies produced by
`scripts/install-agent-skills.sh`). Edit only `skills/`.

## Layer model

Every skill uses the same three tiers:

1. **`common/` shared contract** — invocation rules, the DAG run loop, defaults,
   tool-output rules, guardrail codes, visual QA. One responsibility per file.
2. **`<skill>/SKILL.md` spine** — the short, always-read entry point: front
   matter, one-line pointers to required reading, `Step 0` confirmation, a
   numbered happy-path workflow (titles + links, not full detail), and handoff.
3. **`<skill>/*.md` leaf pages** — conditional and edge-case detail routed by an
   `[if:...]` tagged router (see `setup.md` style).

## Rules

- **Single canonical home.** Each topic's full text lives in exactly one file.
  Everywhere else is at most one sentence plus a link. Do not paste the same
  paragraph into both `SKILL.md` and a leaf page.
- **Link, do not inline, the preamble.** Never copy `common/preamble.md`
  content (language, Bash/`mdclaw`, no GNU `timeout`, JSON-on-stdout) into a
  `SKILL.md`. Point to it.
- **Separate spine from edge cases.** `SKILL.md` body carries only steps that
  run on every normal invocation. Conditional detail (membrane, isotopes,
  terminal caps, glycans, multi-stage equilibration, custom force, HPC) goes to
  a leaf page. A single numbered step must not bundle many unrelated topics.
- **One numbering scheme per skill.** `SKILL.md` uses `Step 0` (confirm) then
  `1..N`. Leaf pages do NOT continue that numbering; they use section headings
  (`## Solvation`, `## Build Topology`). Never write "Step 4" in a leaf page.
- **Step 0 placement and fields.** Confirm target, `solvent_regime`, and
  `execution_mode` before inspection. Confirm chains/ligands only after
  `inspect_molecules` (call it `Step 0b`). Use the heading `## Step 0: Parse and
  Confirm` in every skill that confirms inputs.
- **Route with `[if:...]` tags.** Skills with leaf pages use a router page
  (`setup.md`-style) listing `[required:always]`, `[required:hitl]`, and
  `[if:<condition>]` entries. Provide an ordered read sequence for the default
  path.
- **Prose walls become tables.** Guardrail/failure codes, restraint options,
  and mode-by-flag command variants are tables, not bullet lists or repeated
  example blocks.
- **State the autonomous default.** Anywhere a skill says "ask the user", also
  give the value to use in `autonomous` mode without asking.
- **One shared stopping rule.** `skills/common/run-loop.md` decides from the
  current request whether to stop or continue at a stage boundary. Stage
  skills link to that rule instead of defining their own auto-chain policy.
  `execution_mode` controls confirmation pauses only; it never selects the
  stopping point.
- **Stable CLI flag names.** Use one flag name for one concept across skills.
  The source-candidate selector passed to `prepare_complex` is
  `--source-candidate-id` everywhere.

## Anti-patterns

- A workflow step longer than ~5 lines that mixes more than one decision.
- A leaf page that re-numbers off the SKILL.md steps.
- The same command block shown in both `SKILL.md` and its leaf page.
- Redirect-only stub pages that add an extra hop. Delete them and repoint
  references to the canonical page.
- "Ask the user" with no autonomous default.
