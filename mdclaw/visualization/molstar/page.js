/* Embedded-data playback for export_molstar_page (Claude Artifacts, plain browsers).
 * Same presentation as the HTTP/MCP viewers; coordinates come from the page. */
'use strict';
async function startMolstarPage() {
    const el = id => document.getElementById(id);
    let playing = false, timer, epoch = 0, current = 0, coordinatesNode, viewer;
    const pause = () => {
        playing = false;
        epoch++;
        clearTimeout(timer);
        el('play').textContent = 'Play';
    };
    const fail = error => {
        pause();
        el('status').className = 'error';
        el('status').textContent = `Unable to display the trajectory: ${error.message || error}`;
        for (const control of el('playback').querySelectorAll('button, input, select')) control.disabled = true;
    };
    try {
        const decode = id => Uint8Array.from(atob(el(id).textContent.trim()), c => c.charCodeAt(0));
        const info = JSON.parse(el('mdclaw-display').textContent);
        const n = info.atom_count, frameCount = info.frame_count;
        const frames = decode('mdclaw-frames');
        if (frames.length !== n * 12 * frameCount) throw new Error('Embedded coordinates are incomplete.');
        const xyz = new Float32Array(frames.buffer, frames.byteOffset, frames.length / 4);
        const frameAt = index => {
            const base = index * n * 3;
            return { elementCount: n, time: { value: index, unit: 'step' }, xyzOrdering: { isIdentity: true },
                x: xyz.subarray(base, base + n), y: xyz.subarray(base + n, base + 2 * n), z: xyz.subarray(base + 2 * n, base + 3 * n) };
        };
        el('files').textContent = [info.structure_name, info.trajectory_name].filter(Boolean).join(' + ');
        el('page-title').textContent = info.page_title || '';
        if (typeof molstar === 'undefined') throw new Error('Mol* did not load from cdn.jsdelivr.net. Check the network and reload.');
        el('status').textContent = 'Loading structure…';
        viewer = await molstar.Viewer.create('viewer', {
            extensions: [], customFormats: [], layoutIsExpanded: false,
            layoutShowControls: false, layoutShowRemoteState: false,
            layoutShowLeftPanel: false, layoutShowLog: false,
            viewportShowExpand: false, viewportShowAnimation: false,
            viewportShowTrajectoryControls: false,
            // Mol* parses this as a '#rrggbb' string; a number throws.
            volumeStreamingDisabled: true, viewportBackgroundColor: '#ffffff'
        });
        if (!viewer.plugin.canvas3d) throw new Error('WebGL is unavailable in this viewer.');
        const plugin = viewer.plugin;
        plugin.canvas3d.setProps({ camera: { mode: 'orthographic' } });
        const pdb = await new Response(new Blob([decode('mdclaw-pdb')]).stream()
            .pipeThrough(new DecompressionStream('gzip'))).text();
        const data = await plugin.builders.data.rawData({ data: pdb, label: info.structure_name });
        const trajectory = await plugin.builders.structure.parseTrajectory(data, 'pdb');
        const preset = await mdclawPresentation(plugin, trajectory, info);
        const model = preset.model;
        if (!model?.obj || !preset.structure?.obj) throw new Error('Mol* could not create the structure.');
        if (model.obj.data.atomicHierarchy.atoms._rowCount !== n) throw new Error('Mol* topology atom count differs from the embedded frames.');
        const stride = info.display?.stride || 1;
        const showFrame = () => {
            el('frame').value = String(current + 1);
            el('frame-label').textContent = `${current + 1} / ${frameCount} · source ${current * stride + 1}`;
        };
        if (frameCount > 1) {
            coordinatesNode = await plugin.build().to(model).insert(
                molstar.lib.plugin.StateTransforms.Model.ModelWithCoordinates,
                { frameIndex: 0, frameCount, atomicCoordinateFrame: frameAt(0) }
            ).commit();
        }
        el('status').textContent = `${n.toLocaleString()} atoms · ${frameCount.toLocaleString()} frame${frameCount === 1 ? '' : 's'} · Drag to rotate; scroll to zoom`;
        el('playback').hidden = frameCount <= 1;
        if (frameCount <= 1) return;
        let pending = null, updating = null;
        const seek = index => {
            pending = ((index % frameCount) + frameCount) % frameCount;
            if (!updating) updating = (async () => {
                try {
                    // Keep only the latest request while a geometry update runs.
                    while (pending !== null) {
                        const next = pending;
                        pending = null;
                        await plugin.build().to(coordinatesNode).update({
                            frameIndex: next, frameCount, atomicCoordinateFrame: frameAt(next)
                        }).commit();
                        current = next;
                        showFrame();
                    }
                } finally { updating = null; }
            })();
            return updating;
        };
        const tick = async run => {
            if (!playing || run !== epoch) return;
            const started = performance.now();
            try { await seek(current + 1); } catch (error) { fail(error); return; }
            // The selected rate is a target: the update time counts toward each interval.
            const interval = 1000 / Number(el('speed').value || 30);
            if (playing && run === epoch) timer = setTimeout(() => tick(run), Math.max(0, interval - (performance.now() - started)));
        };
        el('play').onclick = () => {
            if (playing) pause();
            else { playing = true; el('play').textContent = 'Pause'; tick(++epoch); }
        };
        const move = async index => {
            pause();
            try { await seek(index); } catch (error) { fail(error); }
        };
        el('previous').onclick = () => move(current - 1);
        el('next').onclick = () => move(current + 1);
        el('frame').oninput = event => move(Number(event.target.value) - 1);
        el('frame').max = String(frameCount);
        showFrame();
        document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
        window.addEventListener('pagehide', () => { pause(); viewer.dispose(); });
    } catch (error) { fail(error); }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startMolstarPage);
else startMolstarPage();
