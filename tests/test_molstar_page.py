"""Self-contained Mol* page: same display data as the viewers, embedded safely."""

import base64
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.visualization.molstar_display import DisplayData
from mdclaw.visualization.molstar_page import export_molstar_page
from mdclaw.visualization.molstar_viewer import _ASSETS
from tests.test_molstar_display import system_pair  # noqa: F401  (pytest fixture)


def blocks(page):
    return {m['id']: m['body'] for m in re.finditer(
        r'<script type="[^"]+" id="(?P<id>mdclaw-[a-z]+)">(?P<body>.*?)</script>', page, re.S)}


def test_page_embeds_the_viewer_stream(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    before = [hashlib.sha256(p.read_bytes()).digest() for p in (pdb, dcd)]
    result = export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'out.html'), stage='prod')
    assert result['success'] is True, result
    page = (tmp_path / 'out.html').read_text()
    data = blocks(page)
    info = json.loads(data['mdclaw-display'])
    display = DisplayData(pdb, dcd, stage='prod')
    assert info['atom_count'] == 8 and info['frame_count'] == 7
    assert info['display']['wrap'] == 'applied' and info['display']['water_removed'] == 3
    assert base64.b64decode(data['mdclaw-frames']) == b''.join(display.read(i) for i in range(7))
    assert gzip.decompress(base64.b64decode(data['mdclaw-pdb'])).decode() == display.topology_text
    assert result['page_bytes'] == (tmp_path / 'out.html').stat().st_size
    assert before == [hashlib.sha256(p.read_bytes()).digest() for p in (pdb, dcd)]


def test_page_is_artifact_ready_and_pins_bundled_molstar(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'), title='A <b> & B')
    page = (tmp_path / 'p.html').read_text()
    # claude-artifacts refuses a page that does not begin as a document.
    assert page.startswith('<!DOCTYPE html>\n<html lang="en">\n<head>')
    assert '<title>A &lt;b> &amp; B</title>' in page
    assert page.index('</head>') < page.index('<body>') < page.index('id="mdclaw-frames"')
    assert page.endswith('</body>\n</html>\n')
    source = gzip.decompress((_ASSETS / 'vendor/molstar.js.gz').read_bytes())
    sri = 'sha384-' + base64.b64encode(hashlib.sha384(source).digest()).decode()
    assert f'src="https://cdn.jsdelivr.net/npm/molstar@5.13.0/build/viewer/molstar.js" integrity="{sri}"' in page
    # Only the code block and the pinned Mol* tag are executable scripts.
    scripts = re.findall(r'<script(?: [^>]*)?>', page)
    assert sum('type=' not in s for s in scripts) == 2
    assert 'function mdclawPresentation' in page and 'function startMolstarPage' in page


def test_escaped_data_cannot_close_its_block(tmp_path):
    from mdclaw.visualization.molstar_page import _data_block

    block = _data_block('mdclaw-display', 'application/json', json.dumps({'x': '</script><script>alert(1)'}))
    assert block.count('</script>') == 1
    assert json.loads(block[block.index('>') + 1:-len('</script>')])['x'] == '</script><script>alert(1)'


def test_production_frames_are_sampled_to_the_byte_budget(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    per_frame = 8 * 12 * 4 // 3
    result = export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'),
                                 stage='prod', max_bytes=3 * per_frame)
    assert result['success'] is True
    assert result['frame_count'] == 3 and result['display']['stride'] == 3


def test_budget_failures_are_structured(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    tiny = export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'), max_bytes=10)
    assert tiny['success'] is False and tiny['code'] == 'molstar_page_too_large'
    prep = export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'),
                               stage='solv', max_bytes=11 * 16 * 3)
    assert prep['code'] == 'molstar_page_too_large'
    assert not (tmp_path / 'p.html').exists()


@pytest.mark.parametrize('kwargs, code', [
    ({'max_bytes': 0}, 'invalid_parameter_value'),
    ({'stage': 'bogus'}, 'molstar_input_invalid'),
])
def test_invalid_options(system_pair, tmp_path, kwargs, code):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    result = export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'), **kwargs)
    assert result['success'] is False and result['code'] == code


def test_refuses_to_overwrite_inputs(system_pair):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    result = export_molstar_page(str(pdb), str(dcd), output_file=str(dcd))
    assert result['success'] is False and result['code'] == 'invalid_parameter_value'


def _job(tmp_path, pdb, dcd, status='running'):
    """topo_001 -> prod_001, with prod_001 as a running node leaves it on disk."""
    job = tmp_path / 'job'
    topology = job / 'nodes/topo_001/artifacts/system.topology.pdb'
    topology.parent.mkdir(parents=True)
    shutil.copy2(pdb, topology)
    trajectory = job / 'nodes/prod_001/artifacts/trajectory.dcd'
    trajectory.parent.mkdir(parents=True)
    shutil.copy2(dcd, trajectory)
    (job / 'nodes/topo_001/node.json').write_text(json.dumps(
        {'node_id': 'topo_001', 'node_type': 'topo', 'status': 'completed', 'parent_node_ids': [],
         'artifacts': {'topology_pdb': 'artifacts/system.topology.pdb'}}))
    (job / 'nodes/prod_001/node.json').write_text(json.dumps(
        {'node_id': 'prod_001', 'node_type': 'prod', 'status': status, 'parent_node_ids': ['topo_001'],
         'artifacts': {}}))
    (job / 'progress.json').write_text(json.dumps({'schema_version': 3, 'nodes': {
        'topo_001': {'type': 'topo', 'status': 'completed', 'parents': []},
        'prod_001': {'type': 'prod', 'status': status, 'parents': ['topo_001']}}}))
    return job


def test_running_production_shows_its_frames_so_far(system_pair, tmp_path, monkeypatch):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    job = _job(tmp_path, pdb, dcd)
    monkeypatch.chdir(tmp_path)
    result = export_molstar_page(job_dir=str(job), node_id='prod_001')
    assert result['success'] is True, result
    assert result['in_progress'] is True and result['node_status'] == 'running'
    assert result['display']['stage'] == 'prod'
    assert result['output_file'] == str(tmp_path / 'job_prod_001_molstar.html')
    assert 'running' in result['title'] and 'republish' in result['next_action']


def test_finished_node_is_not_reported_in_progress(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    job = _job(tmp_path, pdb, dcd, status='completed')
    result = export_molstar_page(job_dir=str(job), node_id='prod_001', output_file=str(tmp_path / 'p.html'))
    assert result['success'] is True and result['in_progress'] is False
    assert 'running' not in result['title'] and 'republish' not in result['next_action']


def test_a_completed_node_without_a_trajectory_shows_its_structure(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    job = _job(tmp_path, pdb, dcd)
    result = export_molstar_page(job_dir=str(job), node_id='topo_001', output_file=str(tmp_path / 'p.html'))
    assert result['success'] is True and result['display']['stage'] == 'topo'
    assert result['trajectory_name'] is None


def test_a_source_node_refuses_its_mmcif_with_a_route(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    job = _job(tmp_path, pdb, dcd)
    node = json.loads((job / 'nodes/topo_001/node.json').read_text())
    (job / 'nodes/topo_001/artifacts/5AWL.cif').write_text('data_5AWL\n')
    node.update(node_type='source', artifacts={'structure_file': 'artifacts/5AWL.cif'})
    (job / 'nodes/topo_001/node.json').write_text(json.dumps(node))
    result = export_molstar_page(job_dir=str(job), node_id='topo_001', output_file=str(tmp_path / 'p.html'))
    assert result['success'] is False and result['code'] == 'molstar_node_not_ready'
    assert 'prep' in ' '.join(result['hints'])


def test_a_running_stage_without_coordinates_points_at_its_parent(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    job = _job(tmp_path, pdb, dcd)
    (job / 'nodes/prod_001/artifacts/trajectory.dcd').unlink()
    result = export_molstar_page(job_dir=str(job), node_id='prod_001', output_file=str(tmp_path / 'p.html'))
    assert result['success'] is False and result['code'] == 'molstar_node_not_ready'
    assert result['next_action'].endswith('--node-id topo_001 export_molstar_page')


@pytest.mark.parametrize('kwargs,code', [
    ({}, 'invalid_parameter_value'),
    ({'job_dir': 'somewhere'}, 'invalid_parameter_value'),
    ({'job_dir': 'somewhere', 'node_id': 'prod_001'}, 'molstar_node_not_found'),
])
def test_input_selection_errors(kwargs, code):
    result = export_molstar_page(**kwargs)
    assert result['success'] is False and result['code'] == code


def test_cli_writes_page(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    out = tmp_path / 'cli.html'
    process = subprocess.run([sys.executable, '-m', 'mdclaw._cli', 'export_molstar_page',
                              '--structure-file', str(pdb), '--trajectory-file', str(dcd),
                              '--output-file', str(out)], capture_output=True, text=True, timeout=120)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)['success'] is True and out.is_file()


def test_page_playback_js():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is needed only for the deterministic page playback tests')
    test = Path(__file__).parent / 'js/molstar_page.test.cjs'
    result = subprocess.run([node, '--test', str(test)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
