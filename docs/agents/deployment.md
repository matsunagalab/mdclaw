# Agent Deployment

MDClaw deployment has two separate concerns:

1. **Agent skill discovery**: how the agent finds the MDClaw skills.
2. **MD runtime execution**: how `mdclaw <tool>` reaches AmberTools, OpenMM,
   Python dependencies, and optional GPU/container support.

Keeping these separate avoids most deployment confusion. Skills help the agent
turn a scientific request into MD actions; conda, SIF, Docker, or a local
install provide the scientific software stack used by the CLI.

## Directory Roles

| Path | Role | Commit Status |
|---|---|---|
| `skills/<name>/SKILL.md` | Source-of-truth MDClaw skills. All harnesses should ultimately read these. | Tracked |
| `.agents/skills/<name>` | Generic Agent Skills discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.claude/skills/<name>` | Repo-local Claude Code skill discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.codex/skills/<name>` | Optional Codex discovery entries created by `npx skills add`. | Regeneratable local mirror |
| `.claude-plugin/` | Claude plugin marketplace metadata. | Tracked distribution metadata |
| `hooks/hooks.json` | Claude plugin lifecycle hooks. SessionStart prepares the packaged MD runtime. | Tracked plugin hook |
| `bin/mdclaw` | Runtime wrapper. Chooses conda, Singularity/Apptainer, Docker, or local CLI. | Tracked executable |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. | Tracked runtime build |

These directories are intentionally not all the same thing. For example,
`.claude-plugin/` does not contain the skills; it tells Claude how to install
the plugin. `.agents/skills/` and `.claude/skills/` are not additional sources
of truth; they are discovery surfaces that mirror `skills/`.

## Deployment Matrix

| User / Harness | Skill Discovery | Runtime Path | Commands |
|---|---|---|---|
| Claude Code plugin user | Plugin exposes `skills/` and `/mdclaw:*` commands | `bin/mdclaw` plus SessionStart runtime setup | `/plugin install mdclaw@mdclaw` |
| Repo-local Claude Code developer | `.claude/skills/` mirrors `skills/` | Usually conda env `mdclaw`; `bin/mdclaw` also works | Open repo, use discovered skills |
| Pi | `package.json` points Pi at `./skills` | Conda, SIF, Docker, or local CLI | `pi install git:github.com/matsunagalab/mdclaw@main` |
| Codex / Cursor / OpenCode / generic skill harness | `.agents/skills/`, `.claude/skills/`, or the harness's own skill directory | Pixi, Conda, SIF, Docker, or local CLI | `npx skills add matsunagalab/mdclaw` |
| Direct CLI user | No skills required | `mdclaw` command in conda/local/container | `mdclaw --list` |

## Claude Code Plugin

Install from the plugin marketplace:

```text
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The plugin install provides `skills/`, `bin/mdclaw`, plugin metadata, and the
SessionStart hook. The hook prepares the packaged MD runtime by running:

```text
scripts/setup-container.sh
```

That script first exits successfully when a matching conda env is already
available. Otherwise it checks the plugin version, then either:

- pulls a Singularity/Apptainer SIF from GHCR when available, or
- pulls the Docker image as a desktop fallback.

The container is only the scientific execution environment for `mdclaw <tool>`.
It is not a second skill source and does not change the prompt-facing MDClaw
skills.

The plugin command namespace is prefixed:

```text
/mdclaw:md-prepare
/mdclaw:md-equilibration
/mdclaw:md-production
/mdclaw:md-analyze
/mdclaw:md-fep
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

## Codex, Cursor, OpenCode, and Generic Agents

MDClaw skills follow the [Agent Skills](https://agentskills.io) layout, so the
standard [skills CLI](https://github.com/vercel-labs/skills) installs them for
any supported harness (Node 22.20+ required). Run it in the project where the
agent works:

```bash
npx skills add matsunagalab/mdclaw
```

Select the skills and target agents when prompted; the shared `common` skill
must be installed alongside the `md-*` skills because they reference its
pages. `npx skills update` picks up later releases, and `--copy` installs
copies instead of symlinks on filesystems that do not support them.

Working inside this checkout needs no installer at all: the repository tracks
relative-symlink mirrors under `.agents/skills` and `.claude/skills`
(`.agents/skills/md-prepare -> ../../skills/md-prepare`), so repo-local
agents discover the skills directly. If the mirrors are damaged — cloud-sync
tools strip symlinks — restore them with `git checkout -- .agents .claude`.

## Runtime Options

`bin/mdclaw` chooses a runtime for each CLI invocation:

1. Explicit `MDCLAW_RUNTIME=pixi|conda|singularity|apptainer|docker`.
2. A pixi env installed in the checkout (`.pixi/envs/default`, created by
   `pixi install`).
3. A conda env named by `MDCLAW_CONDA_ENV`, default `mdclaw`.
4. Singularity with `MDCLAW_SIF`, plugin data SIF, or repo-local `mdclaw.sif`.
5. Apptainer with the same SIF search path.
6. Docker image from `MDCLAW_DOCKER_IMAGE`, default
   `ghcr.io/matsunagalab/mdclaw:<plugin-version>` when the version is known,
   otherwise `ghcr.io/matsunagalab/mdclaw:latest`.
7. A local `mdclaw` executable on `PATH`.

Host-side SLURM tools run natively because they need `sbatch`, `squeue`,
`sinfo`, `sacct`, and `scancel`. Compute-heavy tools run inside the selected
runtime when possible.

SIF and Docker are two forms of the same packaged MD runtime. Use them when
the host does not already provide the conda scientific stack, or when a
reproducible runtime boundary is more useful than a local editable install.

Pick by scenario:

- **Shared lab SIF**: an administrator already provides a SIF; two environment
  variables, no checkout or host Python needed.
- **Pixi**: local development from a checkout; fast, locked installs of the
  same conda-forge stack.
- **Conda**: the legacy local route; works, but has no lockfile and solves
  slowly.
- **Singularity/Apptainer SIF**: recommended for Linux HPC.
- **Docker**: desktop or workstation container runtime; the easiest install on
  macOS.

Container invocations bind the current working directory at the same absolute
path inside the container and set the container working directory to that path.
Agents should still pass paths relative to the current job directory when
possible, but absolute paths under the current working tree resolve the same
way on the host, in Docker, and in Singularity/Apptainer.

### Shared Lab SIF: No User Checkout Required

If the administrator provides `/data/mdclaw.sif`, install the Claude Code or
Pi skills and put these in `~/.bashrc`:

```bash
export MDCLAW_SIF=/data/mdclaw.sif
export MDCLAW_RUNTIME=singularity
```

Work in your own directory, e.g. `~/work`, and invoke the installed SIF CLI:

```bash
singularity exec --env PYTHONPATH= --env PYTHONHOME= "$MDCLAW_SIF" mdclaw --version
```

No external launcher, user checkout, or host Python packages are required.
For SLURM tools, the agent adds standard `--bind` options for the host clients,
libraries, configuration and authentication socket. See
[direct SIF/SLURM invocation](../../skills/hpc-run/sif-slurm.md) for the
tested lab command and how to keep submission-host binds out of queued jobs.

### Pixi Runtime (Recommended For Development)

[Pixi](https://pixi.sh) installs the same conda-forge scientific stack from
the committed lockfile, in seconds rather than minutes, with per-platform
differences handled inside `pixi.toml`:

```bash
curl -fsSL https://pixi.sh/install.sh | sh   # once per machine, no root
cd mdclaw
pixi install                                 # locked env under .pixi/
export PATH="$PWD/bin:$PATH"
mdclaw --list                                # wrapper auto-selects pixi
```

`pixi run <cmd>` (or `pixi shell`) activates the env for direct use, e.g.
`pixi run pytest tests/test_cli.py`. Dependency updates go through
`pixi update` and a committed `pixi.lock` diff, so environment changes have
history. The source-built extras (openmm-torch with PythonTorchForce, PLUMED)
are not conda-installable and still follow
`docs/developer/conda-env-parity-plan.md`.

### Conda Runtime (Legacy)

```bash
conda env create -f environment.yml   # installs this checkout editable (-e .)
export MDCLAW_RUNTIME=conda
export PATH="$PWD/bin:$PATH"
mdclaw --list
```

Use conda only where pixi is unavailable or an existing `mdclaw` env is
already in place. The wrapper invokes the env with `conda run`, so activation
is optional. Note the known platform limits recorded in
`docs/developer/conda-env-parity-plan.md` (no `hole2` on osx-arm64; the
conda `openmm-torch` line is unsolvable next to `openmm>=8.5.1`).

### Singularity / Apptainer Runtime

Use this on HPC systems. Let the plugin hook download a SIF, or pull one:

```bash
singularity pull mdclaw.sif \
  docker://ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_SIF="$PWD/mdclaw.sif"
export MDCLAW_RUNTIME=singularity
export PATH="$PWD/bin:$PATH"
mdclaw --list
```

Use `apptainer pull` and `MDCLAW_RUNTIME=apptainer` instead when that is the
installed command. For NVIDIA GPUs, verify host/container compatibility:

```bash
singularity exec --nv "$MDCLAW_SIF" python -m openmm.testInstallation
```

### Docker Runtime

Docker is the easiest desktop container path. It uses the same GHCR image that
can be converted to a SIF for Singularity/Apptainer on HPC:

```bash
docker pull ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_RUNTIME=docker
export MDCLAW_DOCKER_IMAGE=ghcr.io/matsunagalab/mdclaw:latest
export PATH="$PWD/bin:$PATH"
mdclaw --list
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
- the checkout pixi env, if present (version, OpenMM, AmberTools).
- executable bits on `scripts/*.sh` and `container/scripts/*.sh`
  (cloud-synced checkouts can strip exec bits and symlinks; restore with
  `git checkout`).
- conda env `mdclaw`, if present.
- OpenMM test installation.
- AmberTools executables: `pdb4amber`, `cpptraj`.
- container command availability.
- version sync across Python, plugin, and Pi package metadata.
- `.agents/skills`, `.claude/skills`, and optional `.codex/skills` discovery.
- missing shared `common` skill instructions, broken mirror symlinks, and
  mirror entries missing for any `skills/` directory.

## Common Pitfalls

- **Skills installed but `mdclaw` fails**: skill discovery worked, but the MD
  runtime is missing. Run `scripts/mdclaw-doctor.sh`.
- **Plugin slash commands are available but repo-local short commands are not**:
  plugin commands use `/mdclaw:<skill>`. Repo-local Claude uses
  `.claude/skills/` discovery instead.
- **`.agents/skills` or `.claude/skills` looks duplicated**: these are
  discovery surfaces, usually symlinks to `skills/`.
- **A path works locally but fails in a container**: run from the project or
  job directory and pass paths under that tree. `bin/mdclaw` preserves the
  absolute cwd inside containers, but paths outside the bound cwd may not exist
  there.
- **macOS sandbox agents do not see GPU/OpenCL**: use the conda env from a
  normal terminal or run on HPC for long jobs.
- **Long explicit-water MD on CPU is slow**: use `inspect_openmm_platforms`,
  shorten for smoke tests, or submit to HPC.
