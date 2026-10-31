# Agent Deployment

Start with one of the two paths in the [README](../../README.md#install--deploy):
Pixi on your workstation, or a downloaded SIF on a compute server.
Skills provide instructions to your agent; the runtime provides the scientific
software that executes them. Install skills after preparing the runtime.

## Workstation And Compute Server

### SIF: No User Checkout Required

On an x86_64 (Intel/AMD) Linux server with Singularity installed, download the
MDClaw image and select it:

```bash
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_SIF="$PWD/mdclaw.sif"
export MDCLAW_RUNTIME=singularity
```

The public image is x86_64 only. Pulling converts it to a SIF locally; allow
about 60 GB for temporary files and the result. If `/tmp` or your home directory
is small, [choose a larger temporary/cache location](../developer/container.md#singularity)
before pulling.

If your lab already has a shared SIF, set `MDCLAW_SIF` to its path instead
of downloading another copy.

Work in your own directory, e.g. `~/work`, and invoke the installed SIF CLI:

```bash
singularity exec --nv --bind "$PWD:$PWD" --pwd "$PWD" \
  --env PYTHONPATH= --env PYTHONHOME= "$MDCLAW_SIF" mdclaw --version
singularity exec --nv --bind "$PWD:$PWD" --pwd "$PWD" \
  --env PYTHONPATH= --env PYTHONHOME= "$MDCLAW_SIF" mdclaw inspect_openmm_platforms
```

Run these checks on a GPU node; `--nv` exposes the host NVIDIA GPU to the
container. Use `apptainer` and `MDCLAW_RUNTIME=apptainer` where appropriate.
Keep input/output files under the mounted working directory, or add explicit
binds for other locations. No external launcher, user checkout, or host Python
packages are required. The full doctor below is available with a checkout;
these direct commands verify the image without one.
For SLURM tools, the agent adds standard `--bind` options for the host clients,
libraries, configuration and authentication socket. See
[direct SIF/SLURM invocation](../../skills/hpc-run/sif-slurm.md) for the
tested lab command and how to keep submission-host binds out of queued jobs.

### Pixi Runtime (Workstation And Development)

[Pixi](https://pixi.sh) installs the conda-forge scientific stack from the
committed lockfile, with platform differences handled inside `pixi.toml`:

Install Pixi once with the [official installer](https://pixi.prefix.dev/latest/installation/):

```bash
curl -fsSL https://pixi.sh/install.sh | sh
```

Restart your terminal so `pixi` is on PATH, then:

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw
pixi install                                 # locked env under .pixi/
export PATH="$PWD/bin:$PATH"
scripts/mdclaw-doctor.sh                      # checks the selected runtime
```

Linux x86-64 and ARM64 use the CUDA 12.9 lock (R575 or newer NVIDIA drivers
for OpenMM GPU execution). For older-driver x86-64 nodes such as floyd/n1–n5
(R520–R574), add the `cuda118` environment:

```bash
pixi install -e cuda118     # builds OpenMM 8.6.1 against CUDA 11.8 once (~3 min)
```

It is the same lock with conda-forge's CUDA 12.9 OpenMM replaced by a source
build from `recipes/openmm-cuda118` (build toolkit from NVIDIA's `cuda-11.8.0`
channel label, runtime from conda-forge's `cudatoolkit 11.8`, glibc 2.17 so
Ubuntu 18.04 nodes install it). PyTorch is conda-forge's last CUDA 11.8 GPU
build, 2.5.1, so OpenFF NAGL is held at 0.5.4 (0.5.5+ require PyTorch 2.6); its
charges for the `openff-gnn-am1bcc-1.0.0` model match NAGL 0.6.1 exactly, as do
HPacker structures and pymbar results with the older JAX. OpenCL uses the
host's ICDs through `OCL_ICD_VENDORS=/etc/OpenCL/vendors`, because pixi does
not run `ocl-icd-system`'s post-link script. It also builds, from `recipes/`,
openmm-torch with PythonTorchForce (container commit `b6532900`) against that
OpenMM and PyTorch, and PLUMED 2.9.4 with openmm-plumed 2.1 plus the
container's setBox patch, so PythonTorchForce and PLUMED run on the GPU there
(first install builds them, about 15 minutes). The default environment builds
the same openmm-torch and PLUMED (`recipes/openmm-torch`, `recipes/openmm-plumed`)
against conda-forge's OpenMM 8.6.1 and PyTorch 2.7.1: CUDA plugins on Linux
x86-64/ARM64, OpenCL on macOS. `bin/mdclaw` reads the driver
version from `/proc/driver/nvidia/version` and runs `cuda118` below R575 when
it is installed, otherwise `default` with a warning naming the command above;
`MDCLAW_PIXI_ENV=default|cuda118` overrides. The standard CUDA 11.8 SIF
(conservative driver baseline 520.61.05) remains the packaged alternative;
verify each node with the shared GPU test before replacing its existing SIF.
RIKYU's CUDA 13 image installs the `cuda13` environment from the same lock, published as `ghcr.io/matsunagalab/mdclaw:X.Y.Z-rikyu`. Linux ARM64 MODELLER is available from the
[official generic archive](https://salilab.org/modeller/download_installation.html),
but not from the salilab Conda channel. Install it after `pixi install`:

```bash
pixi run python scripts/install-modeller-arm64.py
```

The installer verifies the pinned archive checksum and refuses to overwrite
existing installations. Supply `KEY_MODELLER` when running MDClaw; the installer
stores no key. The RIKYU image runs the same installer.
On a GPU node, run
`pixi run python container/scripts/test-gpu.py` for the shared calculation test.
The optional PythonTorchForce test is skipped only when the plugin is absent;
container acceptance requires it.

`pixi run <cmd>` (or `pixi shell`) activates the env for direct use, e.g.
`pixi run pytest tests/test_cli.py`. Dependency updates go through
`pixi update` and a committed `pixi.lock` diff, so environment changes have
history. The source-built extras (openmm-torch with PythonTorchForce, PLUMED)
are not conda-installable and still follow
`docs/developer/conda-env-parity-plan.md`.

## Molstar MCP Display

MDClaw 0.6.9 includes the MCP SDK and the same bundled viewer used for remote
playback. `pixi install --locked` installs these with the normal runtime; no
validation directory, separate venv, Node.js or startup molecule is needed.
The installed package and rebuilt container include the same transport.

After preparing the runtime, check the entry point from the checkout:

```bash
bin/mdclaw molstar-mcp --help
```

Register on the **file/compute host**, where the Codex CLI runs, not on the
PC that connects to it. Claude Code displays Molstar through an Artifact page
instead and needs no registration (see below). With a Pixi checkout, register
once from that checkout directory:

```bash
codex mcp add mdclaw_molstar --env MDCLAW_RUNTIME=pixi -- "$PWD/bin/mdclaw" molstar-mcp
```

The absolute launcher path preserves the Pixi manifest location even when the
agent starts from another working directory. **Only the Codex plugin bundles
the `mdclaw_molstar` server:** `.codex-plugin/plugin.json` is the Claude
manifest plus `mcpServers`, and Codex reads it in preference to
`.claude-plugin/plugin.json`. The Claude plugin, Pi and repository-local skills
ship no MCP server; Claude Code shows Molstar as an Artifact page. Checked
2026-10-26 by installing the plugin into empty homes: Codex CLI 0.163.0 lists
`mdclaw_molstar`, Claude Code 2.1.310 reports no MCP servers. If using the
Codex plugin's bundled server, do not register a duplicate; it uses the
existing runtime selector. A plugin cache is a separate checkout: for a
workstation's existing Pixi checkout, use the explicit registration above and
disable the plugin's duplicate server, or prepare Pixi in the plugin checkout.
A SIF supplies the server executable without installing agent skills; keep the
checkout or installed skills available too. Codex's explicit registration also
works when the client does not import that plugin component. UI rendering needs an MCP Apps-capable client; registering
tools alone does not establish that its current screen can render WebGL.

For a shared SIF (0.6.11 or newer) without a checkout, register the installed CLI
instead. Replace the two absolute paths before running on the compute host:

```bash
codex mcp add mdclaw_molstar -- singularity exec \
  --bind /absolute/work:/absolute/work --pwd /absolute/work \
  --env PYTHONPATH= --env PYTHONHOME= \
  /absolute/images/mdclaw.sif mdclaw molstar-mcp
```

Use `apptainer` in place of `singularity` when appropriate. Viewer preparation
runs on the CPU; WebGL runs on the client, so this display-only command does not
need `--nv`. Keep requested files within mounted directories. For Docker use
`docker run --rm -i` with the work directory mounted at the same absolute path;
never allocate a TTY to the stdio server. The normal `bin/mdclaw` runtime wrapper
also selects Pixi/Conda/Singularity/Apptainer/Docker and preserves MCP stdin.
Existing older images lack the SDK: update the image rather than installing
packages into a running shared image.

Open a session that has loaded the registration, or use that client's MCP-only
reconnect action if available. No Codex application restart is needed for
subsequent molecule switches or UI-only updates: call `open_molstar_probe` with
absolute `structure_file` and optional `trajectory_file` paths. A new server
starts with no chosen molecule, so an empty open request reports which input
is missing instead of displaying a sample or another widget's selection.
Existing `mdclaw_molstar_probe` registrations keep working; this installer does
not rewrite them or interrupt active connections.

**Claude Code needs no Molstar MCP registration.** Claude Code (terminal,
Desktop Code tab, Remote Control) does not render MCP Apps widgets: on
2026-10-26 Claude Code 2.1.310 on floyd loaded and called `open_molstar_probe`
successfully, yet never requested the viewer's `ui://` resource, and no panel
appeared in Claude Desktop's Code screen. The display skill therefore writes a
self-contained page with `mdclaw export_molstar_page` on the host and publishes
it as an Artifact. It uses the installed CLI only, so the plugin, Pixi checkout
or shared SIF that runs MDClaw is enough, and it needs no connection from the
desktop to the host: a Claude Desktop outside a supercomputer's firewall can open
the page, including for a production still running. See
[the display skill](../../skills/common/visual-qa.md#check-the-interactive-route).
An existing `mdclaw_molstar` registration in Claude Code is harmless; remove it
with `claude mcp remove mdclaw_molstar -s user` if it is not wanted.

For production use `stage: "prod"`; wrap is on, fit is off, and long previews
keep at most 1,000 frames by default. The UI targets up to 60 fps with bounded
lookahead/cache; it never downloads the source DCD as a file. For preparation,
select the actual assembled PDB with `stage: "topo"`. Ask the agent to use
Molstar and confirm the displayed stage/processing status. See the
[viewer guide](../developer/molstar-viewer.md) for source/stride semantics,
measured limits and existing-connection compatibility.

## Other Runtime Options

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

Docker is an alternative when you prefer containers on a workstation. It uses
the same GHCR image that can be converted to a SIF for Singularity/Apptainer on HPC:

```bash
docker pull ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_RUNTIME=docker
export MDCLAW_DOCKER_IMAGE=ghcr.io/matsunagalab/mdclaw:latest
export PATH="$PWD/bin:$PATH"
mdclaw --list
```

On Linux with NVIDIA GPUs, `bin/mdclaw` adds `--gpus all` when `nvidia-smi` is
available.

## Runtime Selection

`bin/mdclaw`, `scripts/setup-container.sh`, and `scripts/mdclaw-doctor.sh`
share one selection rule:

1. Explicit `MDCLAW_RUNTIME=pixi|conda|singularity|apptainer|docker`.
2. An installed checkout Pixi environment (`.pixi/envs/default`).
3. An existing Conda environment (`MDCLAW_CONDA_ENV`, default `mdclaw`).
4. Singularity, then Apptainer, then Docker when their commands are installed.
5. An existing standalone `mdclaw` executable on PATH.

Pixi execution uses `--frozen --no-install`; environment changes require an
explicit `pixi install`. Docker execution never pulls an absent image; use
setup to download it first. The selected runtime must work; an invalid override or missing selected
environment is an error, not a request to fall back silently. A container
command with no image still selects that runtime: run setup to obtain the
image. Doctor prints the selection, path/image and the next action on failure.

For SIF execution, `MDCLAW_SIF` selects the exact image path. When unset, use
the existing plugin-data SIF, then the checkout SIF. A missing explicitly
specified SIF never falls back to another image. Existing explicit SIFs are
operator-managed and setup never replaces them. To ensure a shared SIF is used
when Pixi or Conda is also installed, set `MDCLAW_RUNTIME` as well.

Setup only pulls containers; it does not create Pixi/Conda environments.
A version argument or `MDCLAW_FORCE_CONTAINER_SETUP=1` retains the option to
prepare a container alongside a local environment when no runtime is specified.
The explicit `MDCLAW_RUNTIME` choice always wins. `MDCLAW_DOCKER_IMAGE` selects
the image to pull/use (also the source for a newly downloaded SIF); otherwise
the default is `ghcr.io/matsunagalab/mdclaw:<plugin-version>`. Setup reports
how to select a container prepared alongside a local environment.

Slurm submission/control tools run on the host to reach `sbatch`, `squeue`
and related clients. This is an execution exception, not a second scientific
runtime. Container computation calls use the checkout source with the selected
image's dependencies, bind the working directory at its absolute path, and
preserve the working directory. Optional model backends remain separate.

## Verification

With a checkout, run:

```bash
scripts/mdclaw-doctor.sh
```

Doctor reports only the selected runtime's readiness:

- CLI startup and the selected environment/image.
- OpenMM: a real Context, energy calculation and one integration step on each
  registered platform. A GPU plugin that cannot run is reported as unavailable.
- AmberTools: `pdb4amber --help` and `cpptraj -h` must actually start.
- Molstar: MCP SDK/server startup and bundled playback UI; client WebGL remains
  a separate display check.
- Optional features: Torch force and PLUMED import checks, HOLE executable
  discovery, and commands to check separately installed model backends.

CLI or core OpenMM/AmberTools/Molstar failures return a nonzero exit status. A usable
CPU platform is sufficient for core checks; absent GPU/optional features are
reported separately and do not fail them. This is not a full validation of
all scientific workflows. It does not install/pull images, and never diagnoses
an unselected Conda/Pixi environment. The standalone CLI fallback reports its
version with a notice that full dependency checks need a selected environment.

Repository checks are separate: version consistency, executable bits and skill
mirrors. These warnings do not change the core runtime result. Restore missing
tracked files/modes carefully; preserve local edits before using `git checkout`.

Pixi and packaged images have different optional capabilities. The image adds
source-built Torch-force/PLUMED plugins and membrane fixes; Pixi alone does not
promise these. Diagnose the environment you will actually use.

## Claude Code Plugin

No Node.js or `npx` is needed. With Claude Code installed, run in your terminal:

```bash
claude plugin marketplace add matsunagalab/mdclaw
claude plugin install mdclaw@mdclaw --scope user
```

User scope applies across projects. Use `--scope local` for yourself in the
current project, or `--scope project` to share the enablement setting with
collaborators. Start a new session after installation.

Alternatively, add the marketplace and open the install screen inside Claude Code:

```text
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The plugin install provides `skills/`, `bin/mdclaw`, plugin metadata, and the
SessionStart hook. The hook prepares the packaged MD runtime by running:

```text
scripts/setup-container.sh
```

The hook uses the same runtime selection as the launcher and doctor. It keeps
an existing Pixi/Conda environment, preserves an explicitly configured shared
SIF, or downloads the selected container. `MDCLAW_RUNTIME` takes priority;
for example, `MDCLAW_RUNTIME=docker` pulls Docker even if Pixi or Singularity
is installed. A missing selected runtime produces an error with a next step.

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

## Codex Plugin

Use a recent Codex CLI with `codex plugin` support. In your terminal:

```bash
codex plugin marketplace add matsunagalab/mdclaw
codex plugin add mdclaw@mdclaw
```

This uses Codex's plugin installer, with no Node.js or `npx` dependency.
Codex accepts the existing Claude-compatible marketplace and plugin metadata;
both agents use the same `skills/` source, including `common`.
The plugin is installed for your user account. Start a new session after
installation; reopen the desktop app to pick up the marketplace and plugin.
See [OpenAI's plugin guide](https://developers.openai.com/plugins/build/plugins).

Prepare Pixi or SIF first, as in the README. In particular, export
`MDCLAW_SIF` and `MDCLAW_RUNTIME` before starting Codex when reusing a SIF.
Do not install the same MDClaw skills again through `npx` if using the plugin.
For project-scoped skills instead, use the skills CLI below.

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
any supported harness (Node 22.20+ required). Follow the README's
[Node.js installation steps](../../README.md#skills-cli-install-nodejs-first)
first if `npx` is unavailable. Claude Code and Codex can instead use their
plugin installers above, without Node.js. Choose the installation scope.

For the current project, run this in the project where the agent works:

```bash
npx skills add matsunagalab/mdclaw
```

For your user account across projects, run this from any directory:

```bash
npx skills add matsunagalab/mdclaw --global
```

`--global` (or `-g`) uses the selected agents' user skill directories under
your home directory; it does not require administrator privileges.

Select the skills and target agents when prompted; the shared `common` skill
must be installed alongside the `md-*` skills because they reference its
pages. `npx skills update` picks up later releases, and `--copy` installs
copies instead of symlinks on filesystems that do not support them.

Working inside this checkout needs no installer at all: the repository tracks
relative-symlink mirrors under `.agents/skills` and `.claude/skills`
(`.agents/skills/md-prepare -> ../../skills/md-prepare`), so repo-local
agents discover the skills directly. If the mirrors are damaged — cloud-sync
tools strip symlinks — restore them with `git checkout -- .agents .claude`.

## Directory Roles

| Path | Role | Commit Status |
|---|---|---|
| `skills/<name>/SKILL.md` | Source-of-truth MDClaw skills. All harnesses should ultimately read these. | Tracked |
| `.agents/skills/<name>` | Generic Agent Skills discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.claude/skills/<name>` | Repo-local Claude Code skill discovery entries, symlinked to `skills/<name>`. | Tracked symlinks; can be regenerated |
| `.codex/skills/<name>` | Optional Codex discovery entries created by `npx skills add`. | Regeneratable local mirror |
| `.claude-plugin/` | Claude plugin marketplace metadata. | Tracked distribution metadata |
| `.codex-plugin/` | Codex plugin manifest: the Claude manifest plus the Molstar MCP server. | Tracked distribution metadata |
| `hooks/hooks.json` | Claude plugin lifecycle hooks. SessionStart prepares the packaged MD runtime. | Tracked plugin hook |
| `bin/mdclaw` | Runtime wrapper. Uses the shared runtime selection described above. | Tracked executable |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. | Tracked runtime build |

These directories are intentionally not all the same thing. For example,
`.claude-plugin/` does not contain the skills; it tells Claude how to install
the plugin. `.agents/skills/` and `.claude/skills/` are not additional sources
of truth; they are discovery surfaces that mirror `skills/`.

## Deployment Matrix

| User / Harness | Skill Discovery | Runtime Path | Commands |
|---|---|---|---|
| Claude Code plugin user | Plugin exposes `skills/` and `/mdclaw:*` commands | `bin/mdclaw` plus SessionStart runtime setup | `/plugin install mdclaw@mdclaw` |
| Codex plugin user | Plugin exposes the same `skills/`, including `common` | Prepared Pixi or SIF runtime | `codex plugin add mdclaw@mdclaw` after adding the marketplace |
| Repo-local Claude Code developer | `.claude/skills/` mirrors `skills/` | Usually the checkout Pixi environment via `bin/mdclaw` | Open repo, use discovered skills |
| Pi | `package.json` points Pi at `./skills` | Conda, SIF, Docker, or local CLI | `pi install git:github.com/matsunagalab/mdclaw@main` |
| Codex / Cursor / OpenCode / generic skill harness | `.agents/skills/`, `.claude/skills/`, or the harness's own skill directory | Pixi, Conda, SIF, Docker, or local CLI | `npx skills add matsunagalab/mdclaw` |
| Direct CLI user | No skills required | `mdclaw` command in conda/local/container | `mdclaw --list` |

## Common Pitfalls

- **Skills installed but `mdclaw` fails**: skill discovery worked, but the MD
  runtime is missing. Run `scripts/mdclaw-doctor.sh`.
- **Plugin slash commands are available but repo-local short commands are not**:
  plugin commands use `/mdclaw:<skill>`. Repo-local Claude uses
  `.claude/skills/` discovery instead.
- **`.agents/skills` or `.claude/skills` looks duplicated**: these are
  discovery surfaces, usually symlinks to `skills/`.
- **A path works locally but fails in a container**: run from the project or
  job directory and keep inputs there, or add explicit mounts when invoking
  containers directly. The wrapper preserves the absolute working directory.
- **macOS sandbox agents do not see GPU/OpenCL**: run doctor in the
  actual execution environment; use a GPU compute node for long jobs.
- **Long explicit-water MD on CPU is slow**: use `inspect_openmm_platforms`,
  shorten for smoke tests, or submit to HPC.
