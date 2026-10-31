---
title: Molstar viewer with on-demand frames
category: development
updated: 2026-10-26
---

# Mol* viewer

`serve_molstar` displays a PDB, or a matching PDB + DCD, in the desktop
Browser pane of Codex or Claude Code. The default camera is orthographic
(parallel projection), including the MCP prototype. Drag to rotate, scroll to zoom, and
use Play/Pause, Previous/Next, or the frame slider. Mol*'s controls panel
provides representation and colour settings. PyMOL PNG previews remain
available separately.

Agent display selection lives in
[the shared display procedure](../../skills/common/visual-qa.md), reached from
the `common` skill, workflow skills, and repository agent guide. Playback or
interactive rotation requests select Molstar; still-image requests select
PyMOL. The agent checks tools and browser reachability in the current session,
then explains and uses a static fallback if interactive display is unavailable.
This is a skill procedure, not automatic app detection inside the Python server.
It uses existing connections and does not install MCP or set up networking.

## Claude Code: self-contained Artifact page

Claude Code (terminal, Desktop Code tab, Remote Control) does not render MCP
Apps widgets, so `open_molstar_probe` succeeds there without a viewer. The
display skill instead runs, on the host that stores the files:

```bash
mdclaw --job-dir /abs/job --node-id prod_001 export_molstar_page \
  --title "CLN025 prod_001" --output-file /abs/scratch/cln025_prod_001.html
```

(or `--structure-file`, `--trajectory-file`, `--stage` for a file outside a DAG)
and publishes `output_file` as an Artifact. The page needs no connection from the
display client to the host, so a desktop outside the site firewall can open it.
A running production shows the frames written so far; republishing with the
previous `slug` and `base_version` keeps the link. The page reuses the
viewers' `DisplayData` (stage, wrap, water removal, sampling, optional fit),
`index.html`/`viewer.css` markup and `mdclawPresentation`; `page.js` plays the
embedded frames with a 10/20/30/60 fps target (default 30). Data sit in
non-executable `<script type=...>` blocks with `<` escaped: display JSON, the
filtered PDB and all retained frames as base64 planar float32 angstroms. Mol*
5.13.0 loads from jsDelivr with an SRI hash computed from the bundled build
(identical file), because Artifacts only allow scripts from a few CDNs. The
output is an Artifact-ready fragment (title, styles, content, scripts) that
also opens in a browser.

Frames are embedded, so the page size bounds the trajectory. Production
frames are sampled further to fit `max_bytes` (default 8 MB of coordinates,
under the 16 MB Artifact limit) and the result reports `frame_count` and
`display.stride`; preparation stages keep all frames and return
`molstar_page_too_large` when they cannot fit. Coordinates leave the host in a
private claude.ai page, so this route is not used when data must stay on the
host. Verified 2026-10-26 by the user in Claude Desktop's Code screen over
Remote Control: CLN025 prod_001, 166 solute atoms / 100 frames, 387 KB page.

## Stage-aware display

Both transports share `DisplayData`: source PDB/DCD files stay unchanged, and
requested frames are processed on the host. Pass `stage` as
`source`, `prep`, `solv`, `topo`, `min`, `eq`, `prod`, or `analyze`.
The default `auto` recognizes the trajectory's DAG stage (or a static PDB's
stage); for external files it uses `prod` for DCD and `prep` for PDB.
When source-stage context is known, the agent passes it explicitly.

- Preparation: protein/nucleic cartoon, ligand/lipid sticks, ion spheres,
  transparent Gaussian water surface. Static PDBs also show the unit cell.
- Production/analyze: remove water and monatomic ions from both the topology
  sent to the client and frame coordinates, keeping only the solute (protein,
  nucleic acid, ligand) and lipids. `display.ions_removed` reports the count.
- `wrap=True` (default): reconstruct whole molecules and image them around
  polymer anchors using each frame's actual cell. Supports orthorhombic and
  triclinic boxes. Missing cells are reported; incomplete connectivity is rejected
  rather than atom-wrapping a broken molecule. `wrap=False` explicitly disables it.
- `max_frames=1000` (default): production stride is `ceil(N/max_frames)`;
  `max_frames=0` keeps all frames. Preparation retains every frame. The UI reports
  stride, original/output counts and source-frame index, not an invented time.
- `fit=False` (default): no fitting. Explicit `fit=True` iterates a Kabsch fit
  of protein backbone to the retained frames' average, up to 10 passes with
  0.0001 nm tolerance, and reports convergence. Every retained atom follows the
  same rotation/translation. It keeps bounded memory and is done only on request.

The PDB and frame arrays always have the same selected atoms/order. All atoms
are imaged before production water and ion removal. Chemical names are preserved in the
served PDB; MDTraj/OpenMM name aliases are used only for classification/bond
connectivity. PDB CONECT is required for unknown components whose bonds cannot
be determined. The display never writes a new DCD or modifies a DAG node.
The cell is omitted for DCD playback (the PDB cell would be stale in NPT), and
for fitted views (the lattice orientation has changed).

```bash
pixi run mdclaw serve_molstar --structure-file /absolute/path/topology.pdb \
  --trajectory-file /absolute/path/trajectory.dcd --stage prod --max-frames 1000
```

The MCP `open_molstar_probe` accepts the same `stage`, `wrap`, `max_frames` and
`fit` fields, plus either a PDB alone or a matching PDB+DCD pair. Its returned
`display` summary states what was applied; component indices are sent only to
the UI. Older **running** MCP servers do not load new Python code when HTML is
refreshed. The UI labels such a connection Legacy and leaves its previous
viewer working. Enabling these host-side features requires a current MCP
connection; UI refresh alone is insufficient. This does not require changing
the viewer's data files or a Codex app restart as part of the implementation,
but this project does not control the client's MCP reconnection mechanism.

### Existing MCP connection without restarting Codex

A live 0.2 server can also stream a **host-prepared display copy**. Run
`DisplayData` on the original topology/DCD, write its retained coordinates one
frame at a time to a separate host-side DCD, and prepend a receipt to the matching
PDB. The receipt is base64 JSON (`atom_count`, `frame_count`, `display` from
`DisplayData.info`) split into `REMARK 950 MDCLAW_DISPLAY_V1 ` lines. Preserve
source/output hashes in a manifest and never overwrite simulation artifacts.
Call the existing `open_molstar_probe` with that PDB/DCD pair.

The UI verifies receipt counts/component indices and applies the shared stage
representation. It labels this path `Host-prepared · legacy transport`, followed
by the actual wrap/stride/fit settings. This is **not a hot reload of Python**;
new source files require preparing a new display copy while the old connection
remains in use. Full trajectories still stay on the host. Ordinary legacy inputs
without a receipt retain the explicit Legacy warning.

Shared presentation helpers live in `mcp-prototype.js`, because old servers
inline only that filename. The HTTP route `presentation.js` serves the same
file; the MCP entrypoint has no side effects until explicitly called. Do not
split out a required helper without preserving that old-server bundle contract.

On 2026-10-25 the existing native connection accepted the prepared 1AKE real MD
(3,417 atoms, 100 frames, wrap applied, fit off). Codex and all existing MCP
processes kept their original PID/start time. The resource served UI version
0.4.1 without reconnecting. The first prepared PDB retained 15,354 terminators from stripped water and
timed out on the client (728,770 bytes). Filtering those terminators reduced it
to 314,289 bytes without changing atoms/coordinates. The user confirmed that
`1ake-md-100ps-prod-wrapped-compact.pdb` displayed through the existing native
connection. The exact transport limit was not established; this confirmation
is of rendering, not a new playback performance measurement.

## Remote Codex / HPC: keep the trajectory on the host

The DCD stays on the compute host. The browser receives the initial PDB and
bundled viewer assets, then requests only the coordinates of the frame being
shown. Each frame is 12 bytes per atom (three float32 coordinates in angstroms).
There is no full-DCD HTTP endpoint, embedded trajectory HTML, file transfer
step, or trajectory-wide browser cache. Rendering and rotation use WebGL on
the client; this is coordinate streaming, not a remote desktop video stream.

In the installed desktop app 26.1021.22027 (build 21747), localhost navigation
has an automatic port-forward path gated on an SSH host (`kind === "ssh"`).
Its paired-device Remote Control path does not use that forwarder. This was
checked in the installed app bundle, not verified on a second client. The
[official remote guide](https://learn.chatgpt.com/docs/remote-connections)
describes a secure relay for device control, but does not document an API for
MDClaw to add arbitrary HTTP traffic to that relay. Do not infer that a
host-side browser tab will become an interactive client-side tab.

Codex Remote controlling a host does not by itself establish an HTTP path to
that host. `127.0.0.1` in the client's browser addresses the client. Use an
existing SSH connection for the viewer, with the same port at both ends.

On the **compute host**, keep this command running:

```bash
pixi run mdclaw serve_molstar --structure-file /absolute/path/topology.pdb \
  --trajectory-file /absolute/path/trajectory.dcd --port 8765
```

On the **client Mac**, keep this separate command running; replace
`user@login-host` with the existing SSH destination or configured SSH alias:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 user@login-host
```

Then open the host's complete printed `viewer_url` in the client browser.
The tunnel carries the HTML, PDB, and requested frames. It does not copy the
DCD to the Mac. Use the same port in both commands; if occupied, choose another
port at both ends. Existing SSH jump-host settings apply. SSH forwarding must
be permitted by the host. Stop the tunnel and viewer with Ctrl-C when done.

### iOS / authenticated HTTPS

The same viewer can be placed behind an authenticated HTTPS reverse proxy:

```text
iOS Safari -> authenticated HTTPS proxy -> SSH forward -> host viewer
```

A companion machine can provide the proxy and SSH connection without storing
the DCD. It forwards requests to the viewer's loopback port. For an existing
HTTPS endpoint, start the host viewer with its exact public origin:

```bash
pixi run mdclaw serve_molstar --structure-file /absolute/path/topology.pdb \
  --trajectory-file /absolute/path/trajectory.dcd --port 8765 \
  --public-origin https://viewer.example.org
```

`public_origin` only permits that exact Host/Origin and prints an HTTPS
`viewer_url`; **it does not deploy a proxy, create a tunnel, or authenticate
users**. The proxy must protect all routes with authentication, preserve the
complete random URL path, and forward the public Host (or rewrite it to the
local loopback origin). No forwarded-header trust or wildcard origins are
used. Do not expose this server through an anonymous public tunnel.

[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/setup/secure-private-apps/private-web-app/)
or [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) are
possible access layers, depending on available infrastructure. No endpoint
has been deployed for this feature. iOS Safari and actual HPC-to-client
connectivity remain unverified. Whether a link opens in the app or the system
browser is controlled by Codex/Claude, not by MDClaw.

## MCP prototype: use the existing Remote Control tool channel

The [MCP server](../../mdclaw/visualization/molstar_mcp_prototype.py)
and [UI](../../mdclaw/visualization/molstar/mcp-prototype.js) provide another
path: the UI calls a host-side MCP tool for each frame. No HTTP listener or
SSH tunnel is started by this server. Its `ui://` HTML resource embeds only
viewer code, not the PDB or DCD. Topology and coordinates arrive through MCP
tool-result metadata. `open_molstar_probe` accepts `structure_file` and
`trajectory_file` (absolute host paths; trajectory is optional). Each call can
select a different pair without restarting the server or MCP connection.
A standard server starts without a selected molecule. Omitting both opens a
startup default only if one was explicitly supplied on the command line.

From MDClaw 0.6.9, use `mdclaw molstar-mcp` in the normal runtime.
The SDK is a locked dependency; see [standard deployment](../agents/deployment.md#molstar-mcp-display)
for Pixi, plugin registration and SIF commands. The historical module/tool
names remain compatible; no validation venv is required.

Each opening result contains a dataset token. The UI binds to that result
through `ui/notifications/tool-result` and includes the token in every data
request. Opening a second molecule does not change existing viewers. The
UI-only data tool accepts tokens, not file paths. A trajectory that grows keeps
its token and its opening frame count; other input changes are rejected. Call
`open_molstar_probe` again to obtain a new token and see the added frames.

The MCP UI targets up to 60 source frames/s. It measures round-trip time to
adapt concurrent frame requests (at most 32) and the rolling lookahead (at most
64 frames). The decoded cache is bounded by 8 MiB and 256 frames, except that
two frames are allowed for systems whose individual frames are larger. Visited
coordinates remain in that bounded cache for reuse on later loops. A short
trajectory can therefore fit completely after playing through once; the source
DCD is never fetched as a file or eagerly downloaded in full. In-flight responses
and Molstar's rendering state use additional memory.

Play first buffers the forward window. A dry buffer pauses explicitly to refill,
without a catch-up burst or skipped frames. Repeated underruns and measured
geometry-update cost can lower the fixed target (60, 30, 20, 15, ... fps) during
that play run; it does not repeatedly speed up and slow down. Browser animation
frame timestamps pace updates. Pause/hidden views stop queued lookahead;
already-issued calls may finish. Seeking prioritizes the new window and the
most recent seek wins, including during startup buffering.

No interpolated coordinates or additional frame dropping is introduced during
playback; production sampling selects source frames before playback. The
reported fps measures completed retained-frame updates, not GPU refresh rate.
The status also shows the target, mean update/wait time, and cache occupancy.
Host-side stride is a separate, explicit sampling operation.

The server reads UI assets from disk on each resource request, but an already
open widget keeps its loaded JavaScript. After a UI update, call
`open_molstar_probe` again and use the newly attached viewer. UI 0.5.1 initially
shows `playback up to 60 fps`, then `buffering` and the target/measured rates.
Close or pause older widgets to stop their traffic. No Codex or MCP restart is
needed for this UI-only change. The user's earlier report of faster 30 fps
playback was followed by a report of uneven speed over Remote Control; local
verification results below do not establish performance on that actual link.

### Historical prototype registration (not a new installation procedure)

On the original m4 checkout, `mdclaw_molstar_probe` was registered in Codex using
an isolated `.validation/molstar/mcp-env` environment with `mcp==1.30.0` and the
existing scientific environment. The optional SDK has not been added to
MDClaw's normal dependencies. The initial probe used synthetic 1AKE motion.
After the real-MD demonstration, its saved configuration selects
`runs/molstar-md-demo-20261025/jobs/main/nodes/analyze_001/artifacts/1ake-md-100ps.pdb`
and the matching `.dcd` (3,341 protein atoms, 100 frames). Version 0.2 supports
runtime selection as described above. During initial deployment the live 0.1
process continued returning the synthetic pair after the source was updated.
On 2026-10-25 at 16:47 JST, logs show the loaded threads' MCP connections
reinitializing. A subsequent native `open_molstar_probe` call in this same
conversation returned `1ake-md-100ps.pdb`, `1ake-md-100ps.dcd`, 3,341 atoms,
100 frames, and a dataset token, confirming that 0.2 is now active. The desktop
app and app-server PIDs/start times remained unchanged: no app restart was
needed. The trigger for that refresh has not been identified; do not prescribe
an unverified reload command or describe the settings `Restart` button as an
MCP-only restart. No further setup is needed for this already-upgraded session.
The user subsequently confirmed that the native real-MD widget displays and
plays, while reporting slow, choppy playback. UI version 0.3 addresses that
performance issue; remote-client performance of the new UI remains to be
confirmed separately from the local latency tests.

On 2026-10-25, `open_molstar_probe` became available in the conversation and
returned the synthetic 1AKE manifest (1,656 atoms, 60 frames). The user then
confirmed that the attached viewer rotates and plays the trajectory on their
client. This confirms the prototype interaction in the tested session. Earlier
registration/tool-discovery failures are historical; repeating prompts or
starting a new chat is not a verified general remedy.

To open the configured pair, call `open_molstar_probe` from
`mdclaw_molstar_probe`. Use its attached MCP app. A successful probe shows
`MCP connected`, frame position, request count and round-trip time.
Remove this registration with `codex mcp remove mdclaw_molstar_probe`.

Example tool arguments for another molecule (paths belong to the host):

```json
{"structure_file": "/absolute/path/topology.pdb", "trajectory_file": "/absolute/path/trajectory.dcd"}
```

**Verified:** official MCP SDK initialization, resource retrieval, frame calls
and bounds checks; local harness controls; user-confirmed rotation and playback
in the attached Codex viewer. **Not verified:** deployment on a cluster/HPC
Codex CLI host or large-atom-count performance. iOS is outside the current
requested scope. No additional SSH tunnel or public HTTP endpoint was used by
the prototype.

## Same-machine browser: local server

Run from the MDClaw checkout in a persistent terminal:

```bash
pixi run mdclaw serve_molstar --structure-file /absolute/path/topology.pdb
pixi run mdclaw serve_molstar --structure-file /absolute/path/topology.pdb \
  --trajectory-file /absolute/path/trajectory.dcd
```

Run one command at a time. The server prints a JSON line containing
`viewer_url` to stderr as soon as it is ready. Open that **complete URL**,
including its random path. Port 0 (the default) selects a free port. The
process stays running until Ctrl-C; the CLI's final JSON arrives only after
it stops. Closing the page does not stop the server. Agents should retain
the running command's process handle, read readiness from stderr, and stop
it after the viewing session. `--heartbeat-seconds 0` silences CLI heartbeats.

In **Codex desktop**, open `viewer_url` in the built-in Browser panel. The
viewer is an ordinary local web page; it needs no MCP server or plugin.

In **Claude Code desktop**, use a local session on the same machine as the
server. Open Browser with Cmd+Shift+B (macOS) or Ctrl+Shift+B (Windows), then
navigate to `viewer_url`. This also works with an independently started
server. Remote Control/SSH sessions are not the local preview path. See the
[official Browser documentation](https://code.claude.com/docs/en/desktop#browse-external-sites).

For Claude-managed preview lifecycle, a project-local `.claude/launch.json`
can use this configuration; substitute actual absolute input paths and a
`pixi` executable available to the desktop app. Add it to existing
configurations rather than replacing them:

```json
{
  "version": "0.0.1",
  "configurations": [{
    "name": "molstar",
    "runtimeExecutable": "pixi",
    "runtimeArgs": ["run", "mdclaw", "serve_molstar",
      "--structure-file", "/absolute/path/topology.pdb",
      "--trajectory-file", "/absolute/path/trajectory.dcd",
      "--port", "8765", "--heartbeat-seconds", "0"],
    "port": 8765,
    "autoPort": false,
    "url": "http://127.0.0.1:8765"
  }]
}
```

The origin alone deliberately returns 404: navigate to the complete
`viewer_url` from the server log after startup. The random path changes on
every restart; do not put it in `launch.json`. Claude's `url` field permits
only an origin for localhost. See [preview configuration](https://code.claude.com/docs/en/desktop#configure-preview-servers).

## Data and limits

- Input files stay on the host. Mol* 5.13.0 JS/CSS and its licence are
  bundled; no CDN or Node.js is required. Remote viewing needs a live connection
  to the host. The browser receives the PDB once and each requested frame.
- Only the chosen PDB, individual DCD frames, and viewer assets are served,
  on `127.0.0.1` under
  a random URL. Cross-origin reads and directory browsing are disabled.
- Use a DCD and its single-model MD topology PDB without alternate locations.
  Atom counts are checked before startup; **atom order must also match** and
  cannot be verified by counts alone. A DCD may grow while it is viewed: the
  frames counted at opening stay valid, and a partial last frame is ignored.
  A replaced or truncated DCD, or a changed PDB, returns 409; restart the viewer.
- DCD reads are serialized and bounded to one frame. Memory use scales with
  the atom count, not the number of DCD frames. Large atom counts still require
  client graphics memory and 12 bytes per atom per displayed frame of bandwidth.
  A 100,000-atom frame is 1.2 MB; streaming does not eliminate that cost.
- Whole-molecule wrapping is on by default when a cell is available. Production
  removes water and ions and samples at most 1,000 frames by default; fitting is opt-in.
  HTTP playback targets up to 10 frames/s and MCP playback up to 60 frames/s;
  these are display speeds, not simulation time. Input files and the DAG are
  unchanged. Other trajectory formats and desktop hosts are outside this scope.

## Verification

Run `pixi run pytest tests/test_molstar_display.py tests/test_molstar_viewer.py -q` for real PDB/DCD parsing,
HTTP isolation, exact per-frame coordinates and bounded reads, concurrent
seeking, reverse-proxy origin checks, asset integrity, CLI readiness, and shutdown. For
each desktop host, also check PDB alone, PDB+DCD rotation, playback/pause,
and slider endpoints using WebGL. A synthetic trajectory tests display
behaviour only; it is not an MD result. Host verification results are in
[the working memo](../memo.md).

Run `pixi run --locked pytest tests/test_molstar_mcp.py -q`. The required SDK
must be installed; absence fails rather than skips this test. It exercises the
legacy startup pair, standard CLI without initial files, and runtime wrapper
from another working directory. It initializes the real stdio protocol, reads
the bundled UI, switches datasets, checks exact frames and rejects invalid
pairs/tokens. CI runs it on all three Pixi platforms; the container CPU smoke
test also checks the installed MCP service. These protocol tests do not prove
client WebGL rendering on an untested desktop application.

`node --test tests/js/molstar_playback.test.cjs` runs the actual UI code with
mocked Molstar updates and a deterministic clock. It covers ordered playback,
latest-seek wins, pause during buffering, bounded lookahead/concurrency, short
loop cache reuse, stage presets and prepared receipts. With a 10 ms update cost
and 200 ms RTT, playback sustains 60 frames/s after the startup buffer fills.
With RTT varying from 100 to 580 ms, the steady-state median and p99 frame
intervals were both 16.7 ms. A 12/20 ms alternating update cost settles at a
30 fps target. These simulated timings are regression evidence, not real GPU
or Remote Control measurements. The pytest viewer suite invokes these tests
when Node is available.

The earlier UI 0.3 real-Molstar test (3,341 atoms, 200 ms added latency) reached
28–29 updates/s. UI 0.5.1 verification uses the wrapped production system with
3,417 atoms/100 frames. Over about 226 seconds after warmup with 200 ms added
latency, it averaged 53.2 updates/s (median interval 17.2 ms, p95 25.9 ms,
p99 29.3 ms). The cache held all 100 visited coordinate frames and subsequent
loops needed no coordinate requests. This is not a guarantee of constant 60 fps
or a measurement of the actual Remote Control connection.

The user confirmed faster remote playback and no stutters after the first full
pass. They clarified that intermittent first-pass stutters predated this change
and asked not to pursue them as a regression. See [the working memo](../memo.md).
