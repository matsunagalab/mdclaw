# Agent Deployment

MDClaw skills are ordinary Agent Skills under `skills/<name>/SKILL.md`.
The skill names are lowercase hyphenated and match their directory names, so
the same runbooks can be used by Pi, OpenCode, Codex, Claude Code, and other
skill-aware harnesses.

## Pi

Pi can install the skills directly from this repository:

```bash
pi install git:github.com/matsunagalab/mdclaw@main
```

The repository `package.json` declares:

```json
{
  "pi": {
    "skills": ["./skills"]
  }
}
```

This installs the skills. The MDClaw CLI runtime is still provided by one of:

- the `mdclaw` conda environment from `environment.yml`
- the plugin/container wrapper `bin/mdclaw`
- a Singularity/Apptainer SIF through `MDCLAW_SIF`
- Docker through `MDCLAW_DOCKER_IMAGE`

Run `scripts/mdclaw-doctor.sh` after installation to check the runtime.

## OpenCode, Codex, And Generic Agents

Use `.agents/skills` as the common discovery directory:

```bash
git clone https://github.com/matsunagalab/mdclaw
cd mdclaw
scripts/install-agent-skills.sh
scripts/mdclaw-doctor.sh
```

By default, `install-agent-skills.sh` creates relative symlinks from
`.agents/skills/<name>` to `skills/<name>`. If the harness does not follow
symlinks, use:

```bash
scripts/install-agent-skills.sh --copy
```

## Claude Code

Claude Code can use MDClaw in two ways:

1. Install the Claude plugin, which provides slash commands, hooks, and the
   `bin/mdclaw` wrapper.
2. Open this repository directly. In repo-local development mode,
   `.claude/commands/` exposes slash commands such as `/md-prepare`,
   `/md-equilibration`, `/md-production`, `/md-analyze`, and `/hpc-run`.

The portable skills remain the same files under `skills/`.

## Runtime Notes

Agent skill installation and MD runtime installation are separate on purpose.
Skills tell the agent what to run; `bin/mdclaw`, conda, Docker, or
Singularity/Apptainer provide the scientific software stack.

For macOS desktop runs, a normal terminal with the `mdclaw` conda environment
may expose Apple OpenCL, while sandboxed editor agents may not. For long
leaderboard-style MD benchmark runs, prefer a non-sandboxed terminal or an HPC
node. On Linux/NVIDIA HPC, use Singularity/Apptainer with `--nv` and verify
with:

```bash
singularity exec --nv mdclaw.sif python -m openmm.testInstallation
```
