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
    assert base64.b64decode(data['mdclaw-pdb']).decode() == display.topology_text
    assert result['page_bytes'] == (tmp_path / 'out.html').stat().st_size
    assert before == [hashlib.sha256(p.read_bytes()).digest() for p in (pdb, dcd)]


def test_page_is_artifact_ready_and_pins_bundled_molstar(system_pair, tmp_path):  # noqa: F811
    pdb, dcd, _, _ = system_pair
    export_molstar_page(str(pdb), str(dcd), output_file=str(tmp_path / 'p.html'), title='A <b> & B')
    page = (tmp_path / 'p.html').read_text()
    assert page.startswith('<title>A &lt;b> &amp; B</title>')
    assert '<!doctype' not in page.lower() and '<body>' not in page
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
