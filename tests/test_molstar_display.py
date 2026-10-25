"""Scientific display invariants: imaging, selection, sampling and optional alignment."""
import hashlib

import mdtraj as md
import numpy as np
import pytest

from mdclaw.visualization.molstar_display import DisplayData


@pytest.fixture
def system_pair(tmp_path):
    top = md.Topology()
    chain = top.add_chain()
    for name, atoms in [('ALA', ['N', 'CA', 'C', 'O']), ('LIG', ['C1', 'C2']),
                        ('POPC', ['C1', 'C2']), ('HOH', ['O', 'H1', 'H2'])]:
        residue = top.add_residue(name, chain)
        group = [top.add_atom(a, md.element.oxygen if a == 'O' else md.element.hydrogen if a.startswith('H') else md.element.nitrogen if a == 'N' else md.element.carbon, residue) for a in atoms]
        for a, b in zip(group, group[1:]):
            top.add_bond(a, b)
    xyz = np.array([[2.85, 1, 1], [2.97, 1, 1], [3.08, 1.08, 1], [3.12, 1.18, 1],
                    [3.18, 1.1, 1], [3.28, 1.1, 1], [1.5, 1.4, 1.5], [1.6, 1.4, 1.5],
                    [0.1, 0.1, 0.1], [0.15, 0.1, 0.1], [0.15, 0.15, 0.1]], dtype=np.float32)
    frames = np.stack([(xyz + [i * .12, 0, 0]) % 3 for i in range(7)])
    traj = md.Trajectory(frames, top, unitcell_lengths=np.full((7, 3), 3.), unitcell_angles=np.full((7, 3), 90.))
    pdb, dcd = tmp_path/'system.pdb', tmp_path/'system.dcd'
    traj[0].save_pdb(str(pdb))
    traj.save_dcd(str(dcd))
    return pdb, dcd, xyz, traj


def test_wrap_strip_and_sample_preserve_molecules_and_sources(system_pair):
    pdb, dcd, xyz, _ = system_pair
    before = [hashlib.sha256(p.read_bytes()).digest() for p in (pdb, dcd)]
    display = DisplayData(pdb, dcd, stage='prod', max_frames=3)
    assert list(display.indices) == [0, 3, 6]
    assert display.info['atom_count'] == 8
    assert display.info['display']['water_removed'] == 3
    assert display.info['display']['wrap'] == 'applied'
    assert display.info['display']['fit'] == {'enabled': False}
    assert len(display.info['display']['components']['lipid']) == 2
    for i in range(3):
        shown = display.coordinates(i)
        # Broken-by-PBC protein is whole, ligand remains beside its site.
        np.testing.assert_allclose(np.linalg.norm(np.diff(shown[:4], axis=0), axis=1),
                                   np.linalg.norm(np.diff(xyz[:4], axis=0), axis=1), atol=1e-5)
        assert np.linalg.norm(shown[4] - shown[2]) < .2
        np.testing.assert_allclose(shown[:4].mean(axis=0), [1.5, 1.5, 1.5], atol=1e-5)
    assert before == [hashlib.sha256(p.read_bytes()).digest() for p in (pdb, dcd)]
    output = pdb.parent / 'display.pdb'
    output.write_text(display.topology_text)
    assert md.load_pdb(str(output)).n_atoms == 8


def test_preparation_keeps_water_and_all_frames(system_pair):
    pdb, dcd, _, _ = system_pair
    data = DisplayData(pdb, dcd, stage='solv', max_frames=2)
    assert data.info['frame_count'] == 7
    assert data.info['atom_count'] == 11
    assert data.info['display']['components']['water'] == [8, 9, 10]
    raw = DisplayData(pdb, dcd, wrap=False, max_frames=0)
    assert raw.info['frame_count'] == 7
    assert np.linalg.norm(raw.coordinates(0)[2] - raw.coordinates(0)[1]) > 2


def test_triclinic_wrap(system_pair):
    pdb, dcd, _, traj = system_pair
    traj.unitcell_angles = np.tile([80., 90., 75.], (7, 1))
    # Generate the same whole geometry and deliberately wrap in the skew cell.
    _, _, xyz, _ = system_pair
    vectors = traj.unitcell_vectors[0]
    frac = xyz @ np.linalg.inv(vectors)
    traj.xyz[:] = (frac % 1) @ vectors
    traj.save_dcd(str(dcd))
    data = DisplayData(pdb, dcd)
    shown = data.coordinates(0)
    np.testing.assert_allclose(np.linalg.norm(np.diff(shown[:4], axis=0), axis=1),
                               np.linalg.norm(np.diff(xyz[:4], axis=0), axis=1), atol=1e-5)


def test_fit_is_opt_in_and_preserves_ligand_relative_geometry(system_pair):
    pdb, dcd, xyz, traj = system_pair
    frames = []
    for angle in np.linspace(0, 1, 7):
        c, s = np.cos(angle), np.sin(angle)
        frames.append(xyz @ np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) + angle)
    traj.xyz = np.array(frames)
    traj.unitcell_vectors = None
    traj.save_dcd(str(dcd))
    plain = DisplayData(pdb, dcd)
    assert not np.allclose(plain.coordinates(0), plain.coordinates(6))
    fitted = DisplayData(pdb, dcd, fit=True)
    assert fitted.info['display']['fit']['converged']
    np.testing.assert_allclose(fitted.coordinates(0), fitted.coordinates(6), atol=2e-5)
    assert 'CRYST1' not in fitted.topology_text
    np.testing.assert_allclose(np.linalg.norm(fitted.coordinates(4)[4] - fitted.coordinates(4)[2]),
                               np.linalg.norm(xyz[4] - xyz[2]), atol=1e-5)


def test_missing_connectivity_fails_instead_of_splitting_residue(system_pair):
    pdb, dcd, _, _ = system_pair
    pdb.write_text(''.join(line for line in pdb.read_text().splitlines(keepends=True) if not line.startswith('CONECT')))
    with pytest.raises(ValueError, match='connectivity'):
        DisplayData(pdb, dcd)
    assert DisplayData(pdb, dcd, wrap=False).info['display']['wrap'] == 'off'


@pytest.mark.parametrize('options', [{'stage': 'typo'}, {'max_frames': -1}, {'max_frames': True}, {'wrap': 'true'}, {'fit': 'false'}])
def test_invalid_options(system_pair, options):
    with pytest.raises(ValueError):
        DisplayData(*system_pair[:2], **options)


def test_auto_uses_trajectory_stage_over_topology_stage(system_pair, tmp_path):
    import shutil
    pdb, dcd, *_ = system_pair
    topo_dir = tmp_path / 'nodes/topo_001/artifacts'
    prod_dir = tmp_path / 'nodes/prod_001/artifacts'
    topo_dir.mkdir(parents=True)
    prod_dir.mkdir(parents=True)
    topo = topo_dir / 'system.pdb'
    trajectory = prod_dir / 'run.dcd'
    shutil.copyfile(pdb, topo)
    shutil.copyfile(dcd, trajectory)
    assert DisplayData(topo).info['display']['stage'] == 'topo'
    shown = DisplayData(topo, trajectory)
    assert shown.info['display']['stage'] == 'prod'
    assert shown.info['atom_count'] == 8


def test_missing_cell_is_reported_and_source_mutation_is_rejected(system_pair):
    pdb, dcd, _, traj = system_pair
    display = DisplayData(pdb, dcd)
    pdb.write_text(pdb.read_text() + 'REMARK modified\n')
    with pytest.raises(ValueError, match='changed'):
        display.read(0)
    traj.unitcell_lengths = np.zeros((traj.n_frames, 3))
    traj.save_dcd(str(dcd))
    missing = DisplayData(pdb, dcd)
    assert missing.info['display']['wrap'] == 'no cell'
    assert missing.info['display']['warnings']


def test_filtered_pdb_drops_terminators_of_removed_water_chains(tmp_path):
    top = md.Topology()
    protein = top.add_residue('ALA', top.add_chain())
    for name in ['N', 'CA', 'C', 'O']:
        top.add_atom(name, md.element.oxygen if name == 'O' else md.element.carbon, protein)
    for _ in range(3):
        water = top.add_residue('HOH', top.add_chain())
        top.add_atom('O', md.element.oxygen, water)
    pdb = tmp_path / 'solvated.pdb'
    md.Trajectory(np.arange(21, dtype=np.float32).reshape(1, 7, 3) / 10, top).save_pdb(str(pdb))
    assert sum(line.startswith('TER') for line in pdb.read_text().splitlines()) == 4
    display = DisplayData(pdb, stage='prod', wrap=False)
    terminators = [line for line in display.topology_text.splitlines() if line.startswith('TER')]
    assert len(terminators) == 1
    assert 'ALA' in terminators[0]
    assert 'HOH' not in display.topology_text


def test_production_shows_only_solute_and_lipids(tmp_path):
    top = md.Topology()
    chain = top.add_chain()
    groups = []
    for name, atoms in [('ALA', ['N', 'CA', 'C', 'O']), ('POPC', ['C1', 'C2']), ('NA', ['NA']),
                        ('CL', ['CL']), ('HOH', ['O', 'H1', 'H2'])]:
        residue = top.add_residue(name, chain)
        element = {'NA': md.element.sodium, 'CL': md.element.chlorine}
        group = [top.add_atom(a, element.get(a, md.element.oxygen if a == 'O' else md.element.hydrogen
                                             if a.startswith('H') else md.element.nitrogen if a == 'N'
                                             else md.element.carbon), residue) for a in atoms]
        for a, b in zip(group, group[1:]):
            top.add_bond(a, b)
        groups.append(group)
    xyz = np.array([[1, 1, 1], [1.15, 1, 1], [1.3, 1, 1], [1.4, 1.1, 1], [2, 2, 2], [2.15, 2, 2],
                    [.5, .5, .5], [2.5, 2.5, 2.5], [.2, .2, .2], [.25, .2, .2], [.2, .25, .2]], dtype=np.float32)
    traj = md.Trajectory(np.stack([xyz, xyz]), top, unitcell_lengths=np.full((2, 3), 3.), unitcell_angles=np.full((2, 3), 90.))
    pdb, dcd = tmp_path / 'ions.pdb', tmp_path / 'ions.dcd'
    traj[0].save_pdb(str(pdb))
    traj.save_dcd(str(dcd))
    prod = DisplayData(pdb, dcd, stage='prod')
    assert prod.info['atom_count'] == 6
    assert prod.info['display']['water_removed'] == 3 and prod.info['display']['ions_removed'] == 2
    assert prod.info['display']['components']['ion'] == []
    assert len(prod.info['display']['components']['lipid']) == 2
    assert ' NA ' not in prod.topology_text and ' CL ' not in prod.topology_text
    for stage in ('solv', 'topo', 'min', 'eq'):
        kept = DisplayData(pdb, dcd, stage=stage)
        assert kept.info['atom_count'] == 11, stage
        assert kept.info['display']['ions_removed'] == 0 and len(kept.info['display']['components']['ion']) == 2
