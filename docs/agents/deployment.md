# Agent Deployment

MDClaw deployment has two separate concerns:

1. **Agent skill discovery**: how the agent finds the MDClaw runbooks.
2. **MD runtime execution**: how `mdclaw <tool>` reaches AmberTools, OpenMM,
   Python dependencies, and optional GPU/container support.

Keeping these separate avoids most deployment confusion. Skills tell the agent
what to run; conda, SIF, Docker, or a local install provide the scientific
software stack.

## Directory Roles

| Path | Role | Commit Status |
|---|---|---|
| `skills/<name>/SKILL.md` | Source-of-truth runbooks. All harnesses should ultimately read these. | Tracked |
| `.agents/skills/<name>` | Generic Agent Skills discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.claude/skills/<name>` | Repo-local Claude Code skill discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.claude-plugin/` | Claude plugin marketplace metadata. | Tracked distribution metadata |
| `hooks/hooks.json` | Claude plugin lifecycle hooks. SessionStart runs container setup. | Tracked plugin hook |
| `bin/mdclaw` | Runtime wrapper. Chooses conda, Singularity/Apptainer, Docker, or local CLI. | Tracked executable |
| `container/` | Docker/Singularity image build assets. | Tracked runtime build |

These directories are intentionally not all the same thing. For example,
`.claude-plugin/` does not contain the skills; it tells Claude how to install
the plugin. `.agents/skills/` and `.claude/skills/` are not additional sources
of truth; they are discovery surfaces that mirror `skills/`.

## Deployment Matrix

| User / Harness | Skill Discovery | Runtime Path | Commands |
|---|---|---|---|
| Claude Code plugin user | Plugin exposes `skills/` and `/mdclaw:*` commands | `bin/mdclaw` plus SessionStart container setup | `/plugin install mdclaw@mdclaw` |
| Repo-local Claude Code developer | `.claude/skills/` mirrors `skills/` | Usually conda env `mdclaw`; `bin/mdclaw` also works | Open repo, use discovered skills |
| Pi | `package.json` points Pi at `./skills` | Conda, SIF, Docker, or local CLI | `pi install git:github.com/matsunagalab/mdclaw@main` |
| Codex / OpenCode / generic skill harness | `.agents/skills/` mirrors `skills/` | Conda, SIF, Docker, or local CLI | `scripts/install-agent-skills.sh` |
| Direct CLI user | No skills required | `mdclaw` command in conda/local/container | `mdclaw --list` |

## Claude Code Plugin

Install from the plugin marketplace:

```text
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The plugin install provides `skills/`, `bin/mdclaw`, plugin metadata, and the
SessionStart hook. The hook runs:

```text
scripts/setup-container.sh
```

That script checks the plugin version, then either:

- pulls a Singularity/Apptainer SIF from GHCR when available, or
- pulls the Docker image as a desktop fallback.

The plugin command namespace is prefixed:

```text
/mdclaw:md-prepare
/mdclaw:md-equilibration
/mdclaw:md-production
/mdclaw:md-analyze
/mdclaw:hpc-run
```

## Repo-Local Claude Code

When working inside this repository without installing the plugin,
`.claude/skills/` mirrors the canonical `skills/` directory for local skill
discovery. The repo does not track short slash-command wrappers such as
`/md-prepare`; use the discovered skills directly. Install the Claude plugin
when you want the plugin command namespace such as `/mdclaw:md-prepare`.

## Pi

Pi installs skills directly from this repo:

```bash
pi install git:github.com/matsunagalab/mdclaw@main
```

The relevant `package.json` entry is:

```json
{
  "pi": {
    "skills": ["./skills"]
  }
}
```

Pi skill installation does not by itself install AmberTools/OpenMM. Provide a
runtime with one of the methods below.

## Codex, OpenCode, and Generic Agents

For harnesses that discover skills under `.agents/skills` or `.claude/skills`,
use:

```bash
git clone https://github.com/matsunagalab/mdclaw
cd mdclaw
scripts/install-agent-skills.sh
scripts/mdclaw-doctor.sh
```

Default mode creates relative symlinks:

```text
.agents/skills/md-prepare -> ../../skills/md-prepare
.claude/skills/md-prepare -> ../../skills/md-prepare
```

If symlinks are not supported:

```bash
scripts/install-agent-skills.sh --copy
```

The copy mode is useful for restricted filesystems or tools that package
skills into a separate cache.

## Runtime Options

`bin/mdclaw` chooses a runtime for each CLI invocation:

1. Explicit `MDCLAW_RUNTIME=conda|singularity|apptainer|docker`.
2. A conda env named by `MDCLAW_CONDA_ENV`, default `mdclaw`.
3. Singularity with `MDCLAW_SIF`, plugin data SIF, or repo-local `mdclaw.sif`.
4. Apptainer with the same SIF search path.
5. Docker image from `MDCLAW_DOCKER_IMAGE`, default
   `ghcr.io/matsunagalab/mdclaw:latest`.
6. A local `mdclaw` executable on `PATH`.

Host-side SLURM tools run natively because they need `sbatch`, `squeue`,
`sinfo`, `sacct`, and `scancel`. Compute-heavy tools run inside the selected
runtime when possible.

### Conda Runtime

```bash
conda env create -f environment.yml
conda activate mdclaw
pip install -e .
mdclaw --list
```

Use conda for local development and for machines where you control the Python
environment.

### Singularity / Apptainer Runtime

Use this on HPC systems. Let the plugin hook download a SIF, or set:

```bash
export MDCLAW_SIF=/path/to/mdclaw.sif
```

For NVIDIA GPUs, verify host/container compatibility:

```bash
singularity exec --nv "$MDCLAW_SIF" python -m openmm.testInstallation
```

### Docker Runtime

Docker is the easiest desktop container path:

```bash
export MDCLAW_DOCKER_IMAGE=ghcr.io/matsunagalab/mdclaw:latest
bin/mdclaw --list
```

On Linux with NVIDIA GPUs, `bin/mdclaw` adds `--gpus all` when `nvidia-smi` is
available.

## Verification

Run:

```bash
scripts/mdclaw-doctor.sh
```

It checks:

- `bin/mdclaw` availability.
- conda env `mdclaw`, if present.
- OpenMM test installation.
- AmberTools executables: `pdb4amber`, `antechamber`, `parmchk2`, `cpptraj`.
- container command availability.
- `.agents/skills` and `.claude/skills` discovery.

## Common Pitfalls

- **Skills installed but `mdclaw` fails**: skill discovery worked, but the MD
  runtime is missing. Run `scripts/mdclaw-doctor.sh`.
- **Plugin slash commands are available but repo-local short commands are not**:
  plugin commands use `/mdclaw:<skill>`. Repo-local Claude uses
  `.claude/skills/` discovery instead.
- **`.agents/skills` or `.claude/skills` looks duplicated**: these are
  discovery surfaces, usually symlinks to `skills/`.
- **macOS sandbox agents do not see GPU/OpenCL**: use the conda env from a
  normal terminal or run on HPC for long jobs.
- **Long explicit-water MD on CPU is slow**: use `inspect_openmm_platforms`,
  shorten for smoke tests, or submit to HPC.
