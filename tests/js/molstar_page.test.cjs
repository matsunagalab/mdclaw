// Run the embedded-page playback code against a fake clock and Mol* plugin.
// Geometry updates cost 10 ms here; real WebGL is checked in the client.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness({ frames = 100, atomCount = 3, renderCost = 10, withMolstar = true, declaredFrames = frames } = {}) {
    let now = 0, timerId = 0;
    const timers = new Map(), elements = new Map(), rendered = [], listeners = {};
    const timeout = (callback, ms = 0) => {
        const id = ++timerId; timers.set(id, { at: now + Math.max(0, ms), callback }); return id;
    };
    const element = id => {
        if (!elements.has(id)) elements.set(id, { hidden: true, textContent: '', value: '1', className: '',
            querySelectorAll: () => ['play', 'previous', 'next', 'frame', 'speed'].map(element) });
        return elements.get(id);
    };
    // Frame f stores the value f in every coordinate of every atom.
    const coordinates = new Float32Array(frames * atomCount * 3);
    for (let f = 0; f < frames; f++) coordinates.fill(f, f * atomCount * 3, (f + 1) * atomCount * 3);
    const info = { atom_count: atomCount, frame_count: declaredFrames, structure_name: 'test.pdb', trajectory_name: 'test.dcd',
        page_title: 'test page', display: { stage: 'prod', production: true, wrap: 'applied', stride: 2,
            source_frames: frames * 2, water_removed: 0, fit: { enabled: false }, warnings: [],
            components: { polymer: [0, 1, 2], water: [], lipid: [], ion: [], other: [] } } };
    element('mdclaw-display').textContent = JSON.stringify(info);
    element('mdclaw-frames').textContent = Buffer.from(coordinates.buffer).toString('base64');
    element('mdclaw-pdb').textContent = Buffer.from('test topology').toString('base64');
    element('speed').value = '30';
    const model = { obj: { data: { atomicHierarchy: { atoms: { _rowCount: atomCount } } } } };
    const plugin = {
        canvas3d: { setProps() {} },
        builders: { data: { rawData: async ({ data }) => { assert.equal(data, 'test topology'); return {}; } }, structure: {
            createModel: async () => model, createStructure: async () => ({ obj: {} }),
            tryCreateComponentFromExpression: async () => ({}), representation: { addRepresentation: async () => {} },
            tryCreateUnitcell: async () => {}, parseTrajectory: async () => ({})
        } },
        build() {
            let update;
            return { to() { return this; }, insert(_, value) { update = value; return this; },
                update(value) { update = value; return this; },
                commit() { return new Promise(resolve => timeout(() => {
                    for (const axis of ['x', 'y', 'z']) {
                        assert.equal(update.atomicCoordinateFrame[axis].length, atomCount);
                        assert.ok(update.atomicCoordinateFrame[axis].every(v => v === update.frameIndex), 'coordinates must come from that frame');
                    }
                    rendered.push({ index: update.frameIndex, at: now }); resolve({});
                }, renderCost)); }
            };
        }
    };
    let options;
    const context = vm.createContext({ console, Uint8Array, Float32Array, JSON, TextDecoder,
        performance: { now: () => now },
        atob: value => Buffer.from(value, 'base64').toString('binary'),
        setTimeout: timeout, clearTimeout: id => timers.delete(id),
        window: { addEventListener: (name, fn) => { listeners[name] = fn; } },
        document: { hidden: false, readyState: 'loading', getElementById: element,
            addEventListener: (name, fn) => { listeners[name] = fn; } },
        ...(withMolstar ? { molstar: { Viewer: { create: async (_, value) => { options = value; return { plugin, dispose() {} }; } },
            lib: { plugin: { StateTransforms: { Model: { ModelWithCoordinates: {} } } } } } } : {})
    });
    const dir = path.join(__dirname, '../../mdclaw/visualization/molstar');
    vm.runInContext(fs.readFileSync(path.join(dir, 'mcp-prototype.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(dir, 'page.js'), 'utf8'), context);
    assert.equal(typeof listeners.DOMContentLoaded, 'function', 'page must start after the DOM is ready');
    const started = vm.runInContext('startMolstarPage()', context);
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
    await advance(100); await started;
    return { element, rendered, advance, listeners, options: () => options };
}

test('plays embedded frames in order at the selected target rate', async () => {
    const h = await harness();
    assert.equal(h.element('playback').hidden, false);
    assert.equal(h.options().viewportBackgroundColor, '#ffffff', 'Mol* 5.13 needs a hex string background');
    assert.equal(h.element('frame').max, '100');
    const before = h.rendered.length;
    h.element('play').onclick();
    await h.advance(2000);
    const played = h.rendered.slice(before);
    assert.deepEqual(played.map(item => item.index), played.map((_, i) => (i + 1) % 100), 'playback must not skip frames');
    // 30 fps target with a 10 ms update: the update time counts toward the interval.
    assert.ok(played.length >= 55 && played.length <= 61, `${played.length} frames in 2 s at 30 fps`);
    assert.equal(h.element('frame-label').textContent, `${played.at(-1).index + 1} / 100 · source ${played.at(-1).index * 2 + 1}`);
    h.element('play').onclick();
    const paused = h.rendered.length;
    await h.advance(1000);
    assert.ok(h.rendered.length <= paused + 1, 'pause must stop playback');
});

test('faster speed shows more frames, and seeking wraps around', async () => {
    const h = await harness();
    h.element('speed').value = '60';
    const before = h.rendered.length;
    h.element('play').onclick();
    await h.advance(1000);
    assert.ok(h.rendered.length - before >= 55, 'a 60 fps target must beat 30 fps');
    h.element('previous').onclick();
    await h.advance(100);
    h.element('frame').oninput({ target: { value: '1' } });
    await h.advance(100);
    h.element('previous').onclick();
    await h.advance(100);
    assert.equal(h.rendered.at(-1).index, 99);
});

test('a missing Mol* script is reported, not silently blank', async () => {
    const h = await harness({ withMolstar: false });
    assert.equal(h.element('status').className, 'error');
    assert.match(h.element('status').textContent, /Mol\* did not load/);
});

test('incomplete embedded coordinates are refused', async () => {
    const h = await harness({ frames: 2, declaredFrames: 3 });
    assert.equal(h.element('status').className, 'error');
    assert.match(h.element('status').textContent, /incomplete/);
    assert.equal(h.rendered.length, 0);
});
