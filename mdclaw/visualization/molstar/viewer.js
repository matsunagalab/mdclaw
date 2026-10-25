/* The same page runs in Codex and Claude Code; no host-specific APIs. */
'use strict';
(async () => {
    const el = id => document.getElementById(id);
    let playing = false, timer, pending = null, updating = null, epoch = 0;
    let model, coordinatesNode, frameCount = 1, current = 0, viewer;
    const pause = () => {
        playing = false;
        epoch++;
        clearTimeout(timer);
        el('play').textContent = 'Play';
    };
    const fail = error => {
        pause();
        el('status').className = 'error';
        el('status').textContent = `Unable to display files: ${error.message || error}`;
        for (const control of el('playback').querySelectorAll('button, input')) control.disabled = true;
    };
    try {
        const response = await fetch('manifest.json');
        if (!response.ok) throw new Error(`Viewer server returned HTTP ${response.status}. Restart it and open the new URL.`);
        const info = await response.json();
        el('files').textContent = [info.structure_name, info.trajectory_name].filter(Boolean).join(' + ');
        el('status').textContent = 'Loading structure…';
        viewer = await molstar.Viewer.create('viewer', {
            extensions: [], customFormats: [], layoutIsExpanded: false,
            layoutShowControls: false, layoutShowRemoteState: false,
            layoutShowLeftPanel: false, layoutShowLog: false,
            viewportShowExpand: false, viewportShowAnimation: false,
            viewportShowTrajectoryControls: false,
            volumeStreamingDisabled: true, viewportBackgroundColor: '#ffffff'
        });
        if (!viewer.plugin.canvas3d) throw new Error('WebGL is unavailable in this browser.');
        const plugin = viewer.plugin;
        plugin.canvas3d.setProps({ camera: { mode: 'orthographic' } });
        const loadFrame = async index => {
            const response = await fetch(`frames/${index}.bin`, { cache: 'no-store' });
            if (!response.ok) throw new Error(`Frame ${index + 1}: HTTP ${response.status}. Check the connection and finished input files.`);
            const buffer = await response.arrayBuffer();
            if (buffer.byteLength !== info.atom_count * 12) throw new Error('Incomplete coordinate frame.');
            const n = info.atom_count;
            // Mac, Windows and iOS use little-endian float32, matching the wire format.
            const floats = new Float32Array(buffer);
            return { elementCount: n, time: { value: index, unit: 'step' },
                xyzOrdering: { isIdentity: true },
                x: floats.subarray(0, n), y: floats.subarray(n, 2 * n), z: floats.subarray(2 * n) };
        };
        const data = await plugin.builders.data.download({ url: 'structure.pdb', label: info.structure_name });
        const trajectory = await plugin.builders.structure.parseTrajectory(data, 'pdb');
        frameCount = info.frame_count;
        const preset = await mdclawPresentation(plugin, trajectory, info);
        model = preset.model;
        if (!model?.obj || !preset.structure?.obj) throw new Error('Mol* could not create the structure. Check the PDB/DCD pair.');
        if (info.display || info.trajectory_name) {
            el('status').textContent = 'Loading first frame…';
            if (model.obj.data.atomicHierarchy.atoms._rowCount !== info.atom_count) throw new Error('Mol* topology atom count differs from the DCD.');
            coordinatesNode = await plugin.build().to(model).insert(
                molstar.lib.plugin.StateTransforms.Model.ModelWithCoordinates,
                { frameIndex: 0, frameCount, atomicCoordinateFrame: await loadFrame(0) }
            ).commit();
        }
        el('status').textContent = `${info.atom_count.toLocaleString()} atoms · ${frameCount.toLocaleString()} frame${frameCount === 1 ? '' : 's'}${info.trajectory_name ? ' · Loaded on demand' : ''} · Drag to rotate; scroll to zoom`;
        const showFrame = () => {
            el('frame').value = String(current + 1);
            el('frame-label').textContent = `${current + 1} / ${frameCount} · source ${current * (info.display?.stride || 1) + 1}`;
        };
        const seek = index => {
            pending = Math.max(0, Math.min(frameCount - 1, index));
            if (!updating) updating = (async () => {
                try {
                    // Keep the last slider request even while a large frame is updating.
                    while (pending !== null) {
                        const next = pending;
                        pending = null;
                        if (coordinatesNode) {
                            const frame = await loadFrame(next);
                            await plugin.build().to(coordinatesNode).update({
                                frameIndex: next, frameCount, atomicCoordinateFrame: frame
                            }).commit();
                        } else {
                            await plugin.state.data.build().to(model).update({ modelIndex: next }).commit();
                        }
                        current = next;
                        showFrame();
                    }
                } finally { updating = null; }
            })();
            return updating;
        };
        const tick = async run => {
            if (!playing || run !== epoch) return;
            try {
                await seek((current + 1) % frameCount);
                if (playing && run === epoch) timer = setTimeout(() => tick(run), 100);
            } catch (error) { fail(error); }
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
        el('playback').hidden = frameCount <= 1;
        showFrame();
        document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
        window.addEventListener('pagehide', () => { pause(); viewer.dispose(); });
    } catch (error) { fail(error); }
})();
