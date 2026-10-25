// Run the actual UI playback code with a deterministic 200 ms MCP round trip.
// Molstar geometry updates cost 10 ms here; real WebGL is checked separately.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness({ frames = 1000, atomCount = 2, snapshot = false, latency = () => 200, renderCost = () => 10 } = {}) {
    let now = 0, timerId = 0, requests = 0, active = 0, maxActive = 0;
    const timers = new Map(), elements = new Map(), rendered = [], fetched = [], listeners = {};
    const timeout = (callback, ms = 0) => {
        const id = ++timerId; timers.set(id, { at: now + ms, callback }); return id;
    };
    const element = id => {
        if (!elements.has(id)) elements.set(id, { hidden: true, textContent: '', value: 1 });
        return elements.get(id);
    };
    const info = { atom_count: atomCount, frame_count: frames, dataset_id: 'test-dataset',
        structure_name: 'test.pdb', trajectory_name: 'test.dcd', frame_bytes: atomCount * 12 };
    if (snapshot) info.display = { stage: 'topo', production: false, wrap: 'applied',
        stride: 1, source_frames: 1, water_removed: 0, fit: { enabled: false }, warnings: [],
        components: { polymer: [0, 1], water: [], lipid: [], ion: [], other: [] } };

    const parent = { postMessage(message) {
        if (message.method === 'ui/notifications/initialized') {
            timeout(() => deliver({ method: 'ui/notifications/tool-result', params: { structuredContent: info } }));
            return;
        }
        if (!message.id) return;
        if (message.method === 'ui/initialize') {
            timeout(() => deliver({ id: message.id, result: {} })); return;
        }
        assert.equal(message.method, 'tools/call');
        const args = message.params.arguments;
        assert.equal(args.dataset_id, info.dataset_id);
        active++; maxActive = Math.max(maxActive, active);
        if (args.kind === 'frame') fetched.push(args.index);
        timeout(() => {
            active--;
            const coordinates = new Float32Array(atomCount * 3).fill(args.index);
            const data = args.kind === 'structure' ? { info, pdb: 'test topology' } :
                { index: args.index, coordinates: Buffer.from(coordinates.buffer).toString('base64') };
            deliver({ id: message.id, result: { structuredContent: { request: ++requests, bytes: atomCount * 12, host_read_ms: 1 },
                _meta: { mdclaw: { ...data, dataset_id: info.dataset_id } } } });
        }, latency(args, now));
    } };
    function deliver(message) { listeners.message({ source: parent, data: { jsonrpc: '2.0', ...message } }); }
    const plugin = {
        canvas3d: { setProps() {} },
        builders: { data: { rawData: async () => ({}) }, structure: {
            createModel: async () => ({ obj: { data: { atomicHierarchy: { atoms: { _rowCount: atomCount } } } } }),
            createStructure: async () => ({}), tryCreateComponentFromExpression: async () => ({}),
            representation: { addRepresentation: async () => {} }, tryCreateUnitcell: async () => {},
            parseTrajectory: async () => ({}), hierarchy: { applyPreset: async () => ({
                model: { obj: { data: { atomicHierarchy: { atoms: { _rowCount: atomCount } } } } }
            }) }
        } },
        build() {
            let update;
            return { to() { return this; }, insert(_, value) { update = value; return this; },
                update(value) { update = value; return this; },
                commit() { return new Promise(resolve => timeout(() => {
                    // Display only genuine source-frame coordinates, no interpolation.
                    assert.equal(update.atomicCoordinateFrame.x[0], update.frameIndex);
                    rendered.push({ index: update.frameIndex, at: now }); resolve({});
                }, renderCost(update.frameIndex))); }
            };
        }
    };
    const context = vm.createContext({ console, Uint8Array, Float32Array, performance: { now: () => now },
        atob: value => Buffer.from(value, 'base64').toString('binary'),
        setTimeout: timeout, clearTimeout: id => timers.delete(id),
        requestAnimationFrame: fn => timeout(() => fn(now), (Math.floor((now + 0.0001) / (1000 / 60)) + 1) * (1000 / 60) - now),
        cancelAnimationFrame: id => timers.delete(id),
        window: { parent, addEventListener: (name, fn) => { listeners[name] = fn; } },
        document: { hidden: false, getElementById: element, addEventListener: (name, fn) => { listeners[name] = fn; } },
        molstar: { Viewer: { create: async () => ({ plugin, dispose() {} }) },
            lib: { plugin: { StateTransforms: { Model: { ModelWithCoordinates: {} } } } } }
    });
    const source = path.join(__dirname, '../../mdclaw/visualization/molstar/mcp-prototype.js');
    vm.runInContext(fs.readFileSync(source, 'utf8'), context);
    const started = vm.runInContext('startMolstarProbe()', context);
    async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve(); }
    async function advance(ms) {
        const end = now + ms;
        await flush();
        for (;;) {
            const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
            if (!next || next[1].at > end) break;
            now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
        }
        now = end; await flush();
    }
    await advance(1000); await started;
    return { element, rendered, fetched, advance, listeners,
        stats: () => ({ now, requests, active, maxActive }) };
}

test('playback overlaps latency and sustains at least 55 source frames/s after warmup', async () => {
    const h = await harness();
    h.element('play').onclick();
    await h.advance(2500);
    const frames = h.rendered.filter(item => item.at > 1500).length;
    console.log(`200 ms RTT: ${frames / 2} displayed frames/s after warmup`);
    assert.ok(frames >= 110, `only ${frames} frames in two seconds`);
    assert.deepEqual(h.rendered.map(item => item.index), h.rendered.map((_, i) => i), 'playback must not skip source frames');
    assert.ok(h.stats().maxActive <= 32, 'RPC concurrency must be bounded');
    assert.ok(Math.max(...h.fetched) <= h.rendered.at(-1).index + 64, 'prefetch must stay within a small window');
    h.element('play').onclick();
    const pausedAt = h.rendered.length;
    await h.advance(2000);
    assert.ok(h.rendered.length <= pausedAt + 1, 'pause must stop playback');
    assert.ok(h.fetched.length < 250, 'must not download the whole trajectory in the background');
});

test('the most recent seek wins while a previous frame request is pending', async () => {
    const h = await harness();
    h.element('frame').onchange({ target: { value: '901' } });
    await h.advance(5);
    h.element('frame').onchange({ target: { value: '21' } });
    await h.advance(1000);
    assert.equal(h.rendered.at(-1).index, 20);
});

test('looping a short trajectory preserves frame order, and hiding stops requests', async () => {
    const h = await harness({ frames: 12 });
    h.element('play').onclick();
    await h.advance(3000);
    assert.ok(h.rendered.length > 12);
    assert.deepEqual(h.rendered.map(item => item.index), h.rendered.map((_, i) => i % 12));
    h.listeners.pagehide();
    const before = h.fetched.length;
    await h.advance(1000);
    assert.equal(h.fetched.length, before);
});

test('stage presentation keeps lipid/ligand, surfaces preparation water, and hides production water', async () => {
    const source = fs.readFileSync(path.join(__dirname, '../../mdclaw/visualization/molstar/mcp-prototype.js'), 'utf8');
    for (const production of [false, true]) {
        const created = [], represented = [];
        const context = vm.createContext({ document: { getElementById: () => ({ textContent: '' }) } });
        vm.runInContext(source, context);
        const builder = {
            createModel: async () => ({ model: true }), createStructure: async () => ({ structure: true }),
            tryCreateComponentFromExpression: async (_, expression, key) => {
                created.push({ expression, key }); return key;
            },
            representation: { addRepresentation: async (key, options) => { represented.push({ key, options }); } }
        };
        const info = { trajectory_name: 'md.dcd', frame_count: 25, display: {
            stage: production ? 'prod' : 'solv', production, wrap: 'applied', stride: 4,
            source_frames: 100, water_removed: production ? 3 : 0,
            fit: { enabled: false }, warnings: [],
            components: { polymer: [0, 1, 2], water: [3, 4, 5], lipid: [6, 7], ion: [8], other: [9, 10] }
        } };
        await context.mdclawPresentation({ builders: { structure: builder } }, {}, info);
        const water = represented.find(r => r.key === 'mdclaw-water');
        if (production) assert.equal(water, undefined);
        else { assert.equal(water.options.type, 'gaussian-surface'); assert.ok(water.options.typeParams.alpha < .3); }
        assert.equal(represented.find(r => r.key === 'mdclaw-polymer').options.type, 'cartoon');
        for (const kind of ['lipid', 'other']) assert.equal(represented.find(r => r.key === `mdclaw-${kind}`).options.type, 'ball-and-stick');
        assert.deepEqual(Array.from(created.find(c => c.key === 'mdclaw-lipid').expression.args['atom-test'].args[0].args), [6, 7]);
    }
});


test('old MCP consumes a host-prepared receipt and rejects mismatched data', () => {
    const context = vm.createContext({ atob: s => Buffer.from(s, 'base64').toString('binary') });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../mdclaw/visualization/molstar/mcp-prototype.js'), 'utf8'), context);
    const info = { atom_count: 3, frame_count: 2 };
    const display = { stage: 'prod', production: true, wrap: 'applied', stride: 3,
        source_frames: 6, fit: { enabled: false }, warnings: [],
        components: { polymer: [0, 1], water: [], lipid: [2], ion: [], other: [] } };
    const pdb = value => 'REMARK 950 MDCLAW_DISPLAY_V1 ' + Buffer.from(JSON.stringify(value)).toString('base64') + '\nEND\n';
    const result = context.mdclawPreparedDisplay(pdb({ ...info, display }), info);
    assert.equal(result.display.preprocessed, true);
    assert.equal(result.display.stride, 3);
    assert.equal(result.display.fit.enabled, false);
    assert.equal(context.mdclawPreparedDisplay('END', info), info);
    assert.throws(() => context.mdclawPreparedDisplay(pdb({ ...info, atom_count: 4, display }), info), /does not match/);
    assert.throws(() => context.mdclawPreparedDisplay(pdb({ ...info, display: { ...display, components: { polymer: [0, 0, 1] } } }), info), /atom mapping/);
    const current = { ...info, display };
    assert.equal(context.mdclawPreparedDisplay(pdb({ bad: true }), current), current);
});


test('prepared single structure uses PDB positions without a redundant frame RPC', async () => {
    const h = await harness({ frames: 1, snapshot: true });
    assert.equal(h.element('playback').hidden, true);
    assert.match(h.element('status').textContent, /single structure/);
    assert.deepEqual(h.fetched, []);
    assert.equal(h.stats().requests, 1);
});


test('short loops reuse visited coordinates without fetching the DCD up front', async () => {
    const h = await harness({ frames: 100 });
    assert.deepEqual(h.fetched, [0]);
    h.element('play').onclick();
    await h.advance(5000);
    assert.ok(h.rendered.length > 200);
    assert.equal(h.fetched.length, 100, 'each visited frame is fetched once while it fits in bounded cache');
    assert.deepEqual(h.rendered.map(item => item.index), h.rendered.map((_, i) => i % 100));
    h.element('play').onclick();
});


test('variable remote latency is absorbed before playback, with no catch-up bursts', async () => {
    const h = await harness({ latency: args => args.kind === 'frame' ? 100 + (args.index % 7) * 80 : 200 });
    h.element('play').onclick();
    await h.advance(20000);
    const frames = h.rendered.filter(f => f.at > 8000);
    const gaps = frames.slice(1).map((f, i) => f.at - frames[i].at);
    const sorted = gaps.slice().sort((a, b) => a - b);
    console.log(`100–580 ms RTT: median interval ${sorted[Math.floor(sorted.length / 2)].toFixed(1)} ms, p99 ${sorted[Math.floor(sorted.length * .99)].toFixed(1)} ms`);
    assert.ok(frames.length > 600, 'jitter must not sacrifice sustained smooth throughput');
    assert.ok(Math.min(...gaps) >= 16, 'no catch-up burst');
    assert.ok(sorted[Math.floor(sorted.length * .99)] < 34, '99% of intervals should be smooth after buffering');
    assert.deepEqual(h.rendered.map(f => f.index), h.rendered.map((_, i) => i % 1000));
    h.element('play').onclick();
});

test('render cost fluctuating across a vsync boundary settles at a fixed sustainable rate', async () => {
    const h = await harness({ renderCost: index => index % 2 ? 12 : 20 });
    h.element('play').onclick();
    await h.advance(6000);
    assert.match(h.element('status').textContent, /target 30 fps/);
    const frames = h.rendered.filter(f => f.at > 3500);
    const gaps = frames.slice(1).map((f, i) => f.at - frames[i].at);
    assert.ok(Math.max(...gaps) < 42 && Math.min(...gaps) > 25);
    h.element('play').onclick();
});

test('pause or seek during startup buffering cannot resume stale playback', async () => {
    const h = await harness();
    h.element('play').onclick();
    await h.advance(20);
    assert.match(h.element('status').textContent, /buffering/);
    h.element('frame').onchange({ target: { value: '901' } });
    await h.advance(3000);
    assert.equal(h.rendered.at(-1).index, 900);
    assert.equal(h.rendered.length, 2);
    assert.equal(h.element('play').textContent, 'Play');
});
