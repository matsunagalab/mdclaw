/* Shared presentation and MCP entrypoint. Legacy servers inline only this file.
 * HTTP exposes this same file as presentation.js; defining the MCP entrypoint
 * has no side effects. Selection indices match the served PDB. */
'use strict';
async function mdclawPresentation(plugin, trajectory, info) {
    const display = info.display;
    if (!display) {
        // Keep older live MCP connections working, but never imply new processing.
        const status = document.getElementById('processing');
        if (status) status.textContent = 'Legacy MCP connection: stage presets, wrap and sampling are not active. Reconnect the MCP server to enable them.';
        return plugin.builders.structure.hierarchy.applyPreset(trajectory, 'default', {
            structure: { name: 'model', params: {} }, showUnitcell: false
        });
    }
    const builder = plugin.builders.structure;
    const model = await builder.createModel(trajectory);
    const structure = await builder.createStructure(model, { name: 'model', params: {} });
    const expression = indices => ({
        head: { name: 'structure-query.generator.atom-groups' }, args: {
            'atom-test': { head: { name: 'core.set.has' }, args: [
                { head: { name: 'core.type.set' }, args: indices },
                { head: { name: 'structure-query.atom-property.core.source-index' }, args: [] }
            ] }
        }
    });
    const representations = {
        polymer: { type: 'cartoon', color: 'chain-id' },
        water: { type: 'gaussian-surface', typeParams: { alpha: 0.22, resolution: 2, ignoreHydrogens: true }, color: 'uniform', colorParams: { value: 0x87ceeb } },
        lipid: { type: 'ball-and-stick', typeParams: { sizeFactor: 0.15 }, color: 'element-symbol' },
        ion: { type: 'spacefill', typeParams: { sizeFactor: 0.5 }, color: 'element-symbol' },
        other: { type: 'ball-and-stick', color: 'element-symbol' }
    };
    for (const [kind, indices] of Object.entries(display.components)) {
        if (!indices.length || (kind === 'water' && display.production)) continue;
        const component = await builder.tryCreateComponentFromExpression(structure, expression(indices), `mdclaw-${kind}`, { label: kind });
        if (component) await builder.representation.addRepresentation(component, representations[kind]);
    }
    // The PDB holds a single cell. Never draw it as an NPT frame's current cell,
    // or as the cell of a rotated fitted trajectory.
    if ((!info.trajectory_name || info.frame_count === 1) && !display.fit.enabled) await builder.tryCreateUnitcell(model);
    const el = document.getElementById('processing');
    if (el) el.textContent = `${display.preprocessed ? 'Host-prepared · legacy transport · ' : ''}${display.stage} · wrap: ${display.wrap} · stride ${display.stride} (${info.frame_count}/${display.source_frames} frames) · water removed: ${display.water_removed} atoms · fit: ${display.fit.enabled ? 'sampled average' : 'off'}${display.warnings.length ? ' · ' + display.warnings.join('; ') : ''}`;
    return { model, structure };
}

// Older live servers pass PDB bytes through unchanged. A host-prepared display
// copy can carry its processing receipt without changing the server protocol.
function mdclawPreparedDisplay(pdb, info) {
    if (info.display) return info;
    const prefix = 'REMARK 950 MDCLAW_DISPLAY_V1 ';
    const chunks = pdb.split(/\r?\n/).filter(line => line.startsWith(prefix)).map(line => line.slice(prefix.length).trim());
    if (!chunks.length) return info;
    const encoded = chunks.join('');
    if (encoded.length > 8 * 1024 * 1024) throw new Error('Display receipt is too large');
    const receipt = JSON.parse(atob(encoded));
    const d = receipt.display, n = info.atom_count, frames = info.frame_count;
    if (receipt.atom_count !== n || receipt.frame_count !== frames || !d ||
        !['source', 'prep', 'solv', 'topo', 'min', 'eq', 'prod', 'analyze'].includes(d.stage) ||
        typeof d.production !== 'boolean' || !['applied', 'no cell', 'off'].includes(d.wrap) ||
        !Number.isInteger(d.stride) || d.stride < 1 || !Number.isInteger(d.source_frames) ||
        Math.ceil(d.source_frames / d.stride) !== frames ||
        !d.fit || typeof d.fit.enabled !== 'boolean' || !Array.isArray(d.warnings) ||
        !d.warnings.every(v => typeof v === 'string') || !d.components) {
        throw new Error('Display receipt does not match the served trajectory');
    }
    const seen = new Set();
    for (const [kind, indices] of Object.entries(d.components)) {
        if (!['polymer', 'water', 'lipid', 'ion', 'other'].includes(kind) || !Array.isArray(indices)) throw new Error('Invalid display components');
        for (const i of indices) {
            if (!Number.isInteger(i) || i < 0 || i >= n || seen.has(i)) throw new Error('Invalid display atom mapping');
            seen.add(i);
        }
    }
    if (seen.size !== n || (d.production && d.components.water?.length)) throw new Error('Incomplete display atom mapping');
    return { ...info, display: { ...d, preprocessed: true } };
}

/* Throwaway MCP Apps probe. It never fetches HTTP resources or DCD files. */
'use strict';
async function startMolstarProbe() {
    const el = id => document.getElementById(id);
    const waiting = new Map();
    let receiveSelection;
    const selection = new Promise(resolve => { receiveSelection = resolve; });
    let rpcId = 0, playing = false, current = 0, generation = 0, disposed = false;
    let frameRtt = 200, lastTransportUpdate = -Infinity;
    function rpc(method, params) {
        return new Promise((resolve, reject) => {
            const id = ++rpcId;
            const timer = setTimeout(() => { waiting.delete(id); reject(new Error(`${method} timed out`)); }, 30000);
            waiting.set(id, { resolve, reject, timer });
            window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
        });
    }
    window.addEventListener('message', event => {
        if (event.source !== window.parent || event.data?.jsonrpc !== '2.0') return;
        const message = event.data;
        if (message.method === 'ui/notifications/tool-result') {
            receiveSelection(message.params);
            return;
        }
        const request = waiting.get(message.id);
        if (!request) return;
        waiting.delete(message.id); clearTimeout(request.timer);
        if (message.error) request.reject(new Error(message.error.message));
        else request.resolve(message.result);
    });
    await rpc('ui/initialize', { appInfo: { name: 'mdclaw-molstar-prototype', version: '0.5.1' }, appCapabilities: {}, protocolVersion: '2026-01-26' });
    window.parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} }, '*');
    // Bind this view to its opening result, never another widget's selection.
    let selectionTimer;
    const opened = await Promise.race([selection, new Promise((_, reject) => {
        selectionTimer = setTimeout(() => reject(new Error('The host did not send the opening tool result. Open the viewer again.')), 30000);
    })]).finally(() => clearTimeout(selectionTimer));
    if (opened?.isError) throw new Error(opened.content?.[0]?.text || 'Could not open dataset');
    const datasetId = opened?.structuredContent?.dataset_id;
    async function data(args) {
        const started = performance.now();
        const result = await rpc('tools/call', { name: 'molstar_probe_data', arguments: datasetId ? { ...args, dataset_id: datasetId } : args });
        if (result.isError) throw new Error(result.content?.[0]?.text || 'Host tool failed');
        const payload = result._meta?.mdclaw;
        if (!payload) throw new Error('The host did not forward MCP tool metadata to this UI.');
        if (datasetId && payload.dataset_id !== datasetId) throw new Error('Dataset mismatch');
        const stats = result.structuredContent;
        const elapsed = performance.now() - started;
        if (args.kind === 'frame') frameRtt = frameRtt * 0.8 + elapsed * 0.2;
        if (performance.now() - lastTransportUpdate >= 500 || args.kind === 'structure') {
            lastTransportUpdate = performance.now();
            el('transport').textContent = `MCP connected · request ${stats.request} · ${stats.bytes.toLocaleString()} bytes · round trip ${Math.round(performance.now() - started)} ms · host read ${stats.host_read_ms} ms`;
        }
        return payload;
    }
    const initial = await data({ kind: 'structure' });
    const info = mdclawPreparedDisplay(initial.pdb, initial.info);
    el('files').textContent = [info.structure_name, info.trajectory_name].filter(Boolean).join(' + ');
    const viewer = await molstar.Viewer.create('viewer', {
        extensions: [], customFormats: [], layoutIsExpanded: false, layoutShowControls: false,
        layoutShowRemoteState: false, layoutShowLeftPanel: false, layoutShowLog: false,
        viewportShowExpand: false, viewportShowAnimation: false, viewportShowTrajectoryControls: false,
        volumeStreamingDisabled: true, viewportBackgroundColor: '#ffffff'
    });
    if (!viewer.plugin.canvas3d) throw new Error('WebGL is unavailable');
    const plugin = viewer.plugin;
    plugin.canvas3d.setProps({ camera: { mode: 'orthographic' } });
    const raw = await plugin.builders.data.rawData({ data: initial.pdb, label: info.structure_name });
    const trajectory = await plugin.builders.structure.parseTrajectory(raw, 'pdb');
    const preset = await mdclawPresentation(plugin, trajectory, info);
    const n = info.atom_count;
    if (preset.model.obj.data.atomicHierarchy.atoms._rowCount !== n) throw new Error('Topology atom count mismatch');
    // A processed one-frame snapshot already has its exact displayed positions
    // in the PDB. Legacy transport may carry a matching one-frame DCD, but no
    // coordinate request or playback is needed for this static structure.
    if (info.display && info.frame_count === 1) {
        el('status').textContent = `${n.toLocaleString()} atoms · single structure · rotate / zoom`;
        el('playback').hidden = true;
        window.addEventListener('pagehide', () => { disposed = true; viewer.dispose(); });
        return;
    }
    async function readFrame(index) {
        const result = await data({ kind: 'frame', index });
        const bytes = Uint8Array.from(atob(result.coordinates), c => c.charCodeAt(0));
        if (bytes.length !== n * 12 || result.index !== index) throw new Error('Frame mismatch');
        const xyz = new Float32Array(bytes.buffer);
        return { elementCount: n, time: { value: index, unit: 'step' }, xyzOrdering: { isIdentity: true },
            x: xyz.subarray(0, n), y: xyz.subarray(n, 2 * n), z: xyz.subarray(2 * n) };
    }
    // Bound decoded coordinates to 8 MiB (at least two large frames). Retain
    // visited frames for loops; only a short forward window is fetched eagerly.
    const capacity = Math.min(info.frame_count, Math.max(2, Math.min(256, Math.floor(8 * 1024 * 1024 / (n * 12)))));
    const cache = new Map();
    let wanted = [], active = 0, renderQueue = Promise.resolve();
    let targetFps = 60;
    const ahead = () => Math.min(capacity - 1, 64, Math.max(16, Math.ceil(frameRtt * targetFps / 1000) * 3));
    function pump() {
        if (disposed) return;
        const parallel = Math.min(32, Math.max(1, Math.floor(capacity / 2)), Math.ceil(frameRtt * targetFps / 1000) + 2);
        for (const index of wanted) {
            if (active >= parallel) break;
            const entry = cache.get(index);
            if (!entry || entry.started) continue;
            entry.started = true; active++;
            readFrame(index).then(value => { entry.ready = true; entry.resolve(value); }, error => {
                if (cache.get(index) === entry) cache.delete(index);
                entry.reject(error);
            }).finally(() => { active--; pump(); });
        }
    }
    function remove(index, entry) {
        cache.delete(index);
        if (!entry.started) entry.resolve(null);
    }
    function windowAt(index, count) {
        wanted = Array.from({ length: count + 1 }, (_, offset) => (index + offset) % info.frame_count);
        const required = new Set(wanted);
        for (const [key, entry] of cache) {
            if (!required.has(key) && !entry.started) remove(key, entry);
        }
        const missing = wanted.filter(key => !cache.has(key));
        for (const [key, entry] of cache) {
            if (cache.size + missing.length <= capacity) break;
            if (!required.has(key)) remove(key, entry);
        }
        for (const key of missing) {
            let resolve, reject;
            const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
            promise.catch(() => {});
            cache.set(key, { promise, resolve, reject, started: false, ready: false });
        }
        // Promote the actually requested frame, not every speculative frame.
        const entry = cache.get(index);
        cache.delete(index); cache.set(index, entry);
        pump();
    }
    function frame(index, count = 0) {
        windowAt(index, count);
        return cache.get(index).promise;
    }
    const coordinates = await plugin.build().to(preset.model).insert(
        molstar.lib.plugin.StateTransforms.Model.ModelWithCoordinates,
        { frameIndex: 0, frameCount: info.frame_count, atomicCoordinateFrame: await frame(0) }
    ).commit();
    const status = `${n.toLocaleString()} atoms · ${info.frame_count} frames · DCD stays on host`;
    el('status').textContent = `${status} · playback up to 60 fps`;
    let sampleStarted = 0, sampleFrames = 0, updateTotal = 0, waitTotal = 0;
    let animation = 0, lastTickTime = -Infinity, underruns = 0;
    let updateSamples = [];
    const rates = [60, 30, 20, 15, 10, 6, 5, 3, 2, 1];
    function resetSample() {
        sampleStarted = performance.now(); sampleFrames = 0; updateTotal = 0; waitTotal = 0;
        updateSamples = [];
    }
    async function buffer(index, run) {
        windowAt(index, ahead());
        const entries = wanted.map(key => cache.get(key));
        if (entries.some(entry => !entry.ready)) {
            el('status').textContent = `${status} · buffering · target ${targetFps} fps`;
            await Promise.all(entries.map(entry => entry.promise));
        }
        if (disposed || !playing || generation !== run) return false;
        // Restart the clock after buffering, never sprint to catch up.
        lastTickTime = -Infinity; resetSample();
        el('status').textContent = `${status} · target ${targetFps} fps · buffered`;
        return true;
    }
    function show() { el('frame').value = current + 1; el('frame-label').textContent = `${current + 1} / ${info.frame_count} · source ${current * (info.display?.stride || 1) + 1}`; }
    function pause() {
        playing = false; generation++; el('play').textContent = 'Play';
        cancelAnimationFrame(animation); animation = 0; wanted = [];
        // Let existing RPCs finish, but stop queued speculative reads.
        for (const [key, entry] of cache) {
            if (!entry.started) { cache.delete(key); entry.resolve(null); }
        }
    }
    function fail(error) { pause(); el('status').textContent = `Probe failed: ${error.message}`; }
    async function seek(index, run) {
        index = Math.max(0, Math.min(info.frame_count - 1, index));
        const waitingAt = performance.now();
        const atomicCoordinateFrame = await frame(index, playing ? ahead() : 0);
        const waitMs = performance.now() - waitingAt;
        if (disposed || generation !== run || !atomicCoordinateFrame) return;
        const update = renderQueue.then(async () => {
            if (disposed || generation !== run) return;
            const updatingAt = performance.now();
            await plugin.build().to(coordinates).update({ frameIndex: index, frameCount: info.frame_count,
                atomicCoordinateFrame }).commit({ doNotLogTiming: true, doNotUpdateCurrent: true, canUndo: false });
            const updateMs = performance.now() - updatingAt;
            updateTotal += updateMs; waitTotal += waitMs;
            if (playing) updateSamples.push(updateMs);
            if (disposed || generation !== run) return;
            current = index; show();
        });
        renderQueue = update.catch(() => {});
        await update;
    }
    async function tick(run, timestamp) {
        if (!playing || generation !== run) return;
        const started = timestamp;
        if (started - lastTickTime < 1000 / targetFps - 0.5) {
            animation = requestAnimationFrame(timestamp => tick(run, timestamp)); return;
        }
        lastTickTime = started;
        try {
            const next = (current + 1) % info.frame_count;
            if (!cache.get(next)?.ready) {
                // Treat a dry buffer as an explicit pause, not a variable-rate
                // slideshow. Repeated stalls lower the rate for this play run.
                if (++underruns >= 2) {
                    targetFps = rates.find(rate => rate < targetFps) || 1;
                    underruns = 0;
                }
                if (!await buffer(next, run)) return;
                animation = requestAnimationFrame(timestamp => tick(run, timestamp)); return;
            }
            await seek(next, run);
            if (playing && generation === run) {
                sampleFrames++;
                const elapsed = performance.now() - sampleStarted;
                if (elapsed >= 1000) {
                    el('status').textContent = `${status} · target ${targetFps} fps · ${(sampleFrames * 1000 / elapsed).toFixed(1)} fps · update ${(updateTotal / sampleFrames).toFixed(1)} ms · wait ${(waitTotal / sampleFrames).toFixed(1)} ms · cache ${[...cache.values()].filter(e => e.ready).length}/${capacity}`;
                    const sorted = updateSamples.slice().sort((a, b) => a - b);
                    const p90 = sorted[Math.floor((sorted.length - 1) * 0.9)];
                    // Use refresh-friendly fixed rates; only reduce during a
                    // play run so fluctuating costs cannot make speed oscillate.
                    targetFps = rates.find(rate => rate <= targetFps && 1000 / rate >= p90 + 2) || 1;
                    resetSample();
                }
            }
            // Pace by the display clock. Never skip or interpolate source
            // frames; host-side stride is reported separately.
            if (playing && generation === run) animation = requestAnimationFrame(timestamp => tick(run, timestamp));
        } catch (error) { if (generation === run) fail(error); }
    }
    el('play').onclick = () => {
        if (playing) pause();
        else {
            playing = true; targetFps = 60; underruns = 0; resetSample();
            el('play').textContent = 'Pause';
            const run = ++generation;
            buffer((current + 1) % info.frame_count, run).then(ready => {
                if (ready) animation = requestAnimationFrame(timestamp => tick(run, timestamp));
            }).catch(error => { if (generation === run) fail(error); });
        }
    };
    const move = index => {
        pause();
        const run = generation;
        seek(index, run).catch(error => { if (generation === run) fail(error); });
    };
    el('previous').onclick = () => move(current - 1);
    el('next').onclick = () => move(current + 1);
    el('frame').onchange = event => move(Number(event.target.value) - 1);
    el('frame').max = info.frame_count;
    el('playback').hidden = info.frame_count <= 1; show();
    document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
    window.addEventListener('pagehide', () => { pause(); disposed = true; cache.clear(); viewer.dispose(); });
}
