"""Host-side, bounded-memory coordinates and presentation for both Molstar transports."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from mdclaw.visualization._base import _LIPID_RESNAMES

_STAGES = {'auto', 'source', 'prep', 'solv', 'topo', 'min', 'eq', 'prod', 'analyze'}


class DisplayData:
    """Immutable input selection. No source writes or whole-DCD loading."""

    def __init__(self, pdb, dcd=None, *, stage='auto', wrap=True, max_frames=1000, fit=False):
        import mdtraj as md
        from mdclaw.visualization.molstar_viewer import _inspect_inputs, _signature

        if stage not in _STAGES:
            raise ValueError(f'Unknown display stage: {stage}')
        if type(wrap) is not bool or type(fit) is not bool:
            raise ValueError('wrap and fit must be booleans')
        if type(max_frames) is not int or max_frames < 0:
            raise ValueError('max_frames must be a nonnegative integer (0 keeps all frames)')
        self.pdb = Path(pdb).expanduser().resolve(strict=True)
        self.dcd = Path(dcd).expanduser().resolve(strict=True) if dcd else None
        self.signatures = tuple(_signature(p) for p in (self.pdb, self.dcd) if p)
        self.info, _ = _inspect_inputs(str(self.pdb), str(self.dcd) if self.dcd else None)
        # Taken after counting, so every counted frame lies below this size.
        self.dcd_extent = self._dcd_extent()
        self.options = (stage, wrap, max_frames, fit)
        if stage == 'auto':
            # Only recognize the explicit DAG nodes/<type>_<id>/artifacts layout.
            parent = (self.dcd or self.pdb).parent
            node_type = parent.parent.name.split('_')[0] if parent.name == 'artifacts' and parent.parent.parent.name == 'nodes' else ''
            stage = node_type if node_type in _STAGES - {'auto', 'analyze'} else ('prod' if dcd else 'prep')
        self.stage = stage
        production = stage in {'prod', 'analyze'}
        self.original = self.pdb.read_text()
        self.initial = md.load_pdb(str(self.pdb), standard_names=True)
        self.top = self.initial.topology
        if self.top.n_atoms != self.info['atom_count']:
            raise ValueError('PDB parser changed atom count; use an unambiguous MD topology')
        # Treat virtual sites in known waters as part of the water molecule.
        water_names = {'HOH', 'WAT', 'SOL', 'TIP3', 'TIP3P', 'TIP4', 'TIP4P', 'OPC', 'OPC3', 'TP4', 'TP5'}
        waters = {a.index for a in self.top.atoms if a.residue.is_water or a.residue.name.upper() in water_names}
        # Production shows the solute (and lipids): bulk water and monatomic ions are removed.
        ions = {a.index for a in self.top.atoms if a.index not in waters and a.residue.n_atoms == 1
                and not (a.residue.is_protein or a.residue.is_nucleic)}
        hidden = waters | ions if production else set()
        self.keep = np.array([a.index for a in self.top.atoms if a.index not in hidden], dtype=int)
        if not len(self.keep):
            raise ValueError('Production display contains no solute atoms; use stage=solv to inspect water and ions')
        original_count = self.info['frame_count']
        self.stride = max(1, math.ceil(original_count / max_frames)) if production and max_frames else 1
        self.indices = range(0, original_count, self.stride)
        self.wrap = wrap
        self.fit = fit
        self.anchor = self.other = self.sorted_bonds = None
        _, lengths, angles = self._raw(0)
        self.periodic = lengths is not None
        warnings = []
        if wrap and not self.periodic:
            warnings.append('No periodic cell in the input frames; wrap skipped')
        if wrap and self.periodic:
            # OpenMM recognizes terminal H1/H2/H3 and force-field residue aliases
            # that MDTraj may omit. Merge only after verifying coordinate order.
            from openmm.app import PDBFile
            from openmm import unit
            parsed = PDBFile(str(self.pdb))
            positions = np.asarray(parsed.positions.value_in_unit(unit.nanometer))
            if positions.shape != self.initial.xyz[0].shape or not np.allclose(positions, self.initial.xyz[0], atol=1e-5):
                raise ValueError('Topology parsers disagree on atom order; cannot safely wrap')
            known = {tuple(sorted((a.index, b.index))) for a, b in self.top.bonds}
            for a, b in parsed.topology.bonds():
                key = tuple(sorted((a.index, b.index)))
                if key not in known:
                    self.top.add_bond(self.top.atom(a.index), self.top.atom(b.index))
                    known.add(key)
            # Virtual sites have no chemical bond but must follow their water.
            bonded = {a.index for bond in self.top.bonds for a in bond}
            for residue in self.top.residues:
                if not any(a.index in waters for a in residue.atoms):
                    continue
                oxygen = next((a for a in residue.atoms if a.element and a.element.symbol == 'O'), None)
                if oxygen:
                    for atom in residue.atoms:
                        if atom.index not in bonded and atom != oxygen and (atom.element is None or atom.element.symbol == 'VS'):
                            self.top.add_bond(oxygen, atom)
            bonds = list(self.top.bonds)
            molecules = self.top.find_molecules() if bonds else [{a} for a in self.top.atoms]
            member = {a.index: i for i, mol in enumerate(molecules) for a in mol}
            for residue in self.top.residues:
                if len({member[a.index] for a in residue.atoms}) > 1:
                    raise ValueError(f'Incomplete bond connectivity for {residue}; wrap needs a bonded topology (PDB CONECT). Use wrap=false only for an explicitly uncorrected view.')
            polymers = [mol for mol in molecules if any(a.residue.is_protein or a.residue.is_nucleic for a in mol)]
            self.anchor = polymers or [max(molecules, key=len)]
            self.other = [mol for mol in molecules if mol not in self.anchor]
            self.sorted_bonds = self.initial._sort_bonds() if bonds else np.empty((0, 2), dtype=np.int32)
        self.reference = None
        self.align = np.array([i for i, source in enumerate(self.keep)
                               if self.top.atom(int(source)).residue.is_protein
                               and self.top.atom(int(source)).name in {'N', 'CA', 'C', 'O'}], dtype=int)
        fit_info = {'enabled': fit}
        if fit:
            if not dcd or len(self.align) < 3:
                raise ValueError('Average fitting requires a trajectory and at least three protein backbone atoms')
            ref = self._coordinates(0)[self.align].astype(float)
            if np.linalg.matrix_rank(ref - ref.mean(axis=0)) < 2:
                raise ValueError('Backbone atoms are collinear; a stable 3D fit is unavailable')
            converged = False
            for iteration in range(10):
                total = np.zeros_like(ref)
                for source in self.indices:
                    xyz = self._coordinates(source)[self.align]
                    total += self._align(xyz, xyz, ref)
                mean = total / len(self.indices)
                delta = float(np.sqrt(np.mean((mean - ref) ** 2)))
                ref = mean
                if delta < 1e-4:
                    converged = True
                    break
            self.reference = ref
            fit_info.update(reference='iterative sampled average', selection='protein and backbone',
                            converged=converged, iterations=iteration + 1, delta_nm=delta)
            if not converged:
                warnings.append('Average fitting did not converge in 10 iterations')
        lipid_names = set(_LIPID_RESNAMES) | {name[:3] for name in _LIPID_RESNAMES if len(name) == 4}
        groups = {k: [] for k in ('polymer', 'water', 'lipid', 'ion', 'other')}
        for i, source in enumerate(self.keep):
            atom = self.top.atom(int(source))
            residue = atom.residue
            category = ('water' if source in waters else 'polymer' if residue.is_protein or residue.is_nucleic
                        else 'ion' if residue.n_atoms == 1
                        else 'lipid' if residue.name.upper() in lipid_names else 'other')
            groups[category].append(i)
        self.info.update(atom_count=len(self.keep), frame_count=len(self.indices), frame_bytes=len(self.keep) * 12,
                         display={'stage': stage, 'production': production, 'wrap': 'applied' if wrap and self.periodic else 'no cell' if wrap else 'off',
                                  'fit': fit_info, 'stride': self.stride, 'source_frames': original_count,
                                  'source_atoms': self.top.n_atoms, 'water_removed': len(waters) if production else 0,
                                  'ions_removed': len(ions) if production else 0,
                                  'components': groups, 'warnings': warnings})
        self.topology_text = self._pdb_text(self.coordinates(0), lengths, angles)
        self.check_unchanged()

    def summary(self):
        display = dict(self.info['display'])
        groups = display.pop('components')
        display['component_atoms'] = {name: len(indices) for name, indices in groups.items()}
        return {**self.info, 'display': display}

    def _dcd_extent(self):
        if not self.dcd:
            return None
        stat = self.dcd.stat()
        return stat.st_dev, stat.st_ino, stat.st_size

    def check_unchanged(self):
        from mdclaw.visualization.molstar_viewer import _signature
        extent = self._dcd_extent()
        # A running production appends and rewrites the frame count in its header:
        # the frames counted at opening stay valid. Replacing or truncating does not.
        if (_signature(self.pdb) != self.signatures[0] or
                (extent and (extent[:2] != self.dcd_extent[:2] or extent[2] < self.dcd_extent[2]))):
            raise ValueError('Input changed; open the viewer again')

    def _raw(self, index):
        self.check_unchanged()
        if self.dcd:
            from mdtraj.formats import DCDTrajectoryFile
            with DCDTrajectoryFile(str(self.dcd)) as handle:
                handle.seek(index)
                xyz, lengths, angles = handle.read(n_frames=1)
            xyz = xyz.astype(float) / 10.0
            lengths = lengths / 10.0 if lengths is not None else None
        else:
            frame = self.initial[index]
            xyz, lengths, angles = frame.xyz, frame.unitcell_lengths, frame.unitcell_angles
        self.check_unchanged()
        if xyz.shape != (1, self.top.n_atoms, 3) or not np.isfinite(xyz).all():
            raise ValueError('Invalid trajectory coordinates')
        if lengths is not None and (not np.isfinite(lengths).all() or np.any(lengths <= 0) or
                                    angles is None or not np.isfinite(angles).all() or np.any(angles <= 0) or np.any(angles >= 180)):
            raise ValueError('Invalid periodic cell')
        return xyz[0], lengths, angles

    def _coordinates(self, source):
        import mdtraj as md
        xyz, lengths, angles = self._raw(source)
        if self.wrap and self.periodic:
            if lengths is None:
                raise ValueError('Periodic cell disappeared in trajectory')
            frame = md.Trajectory(xyz[None], self.top, unitcell_lengths=lengths, unitcell_angles=angles)
            if not np.isfinite(frame.unitcell_vectors).all() or np.linalg.det(frame.unitcell_vectors[0]) < 1e-8:
                raise ValueError('Degenerate periodic cell')
            frame.image_molecules(inplace=True, anchor_molecules=self.anchor,
                                  other_molecules=self.other, sorted_bonds=self.sorted_bonds)
            xyz = frame.xyz[0]
        return xyz[self.keep]

    @staticmethod
    def _align(xyz, mobile, ref):
        center, target = mobile.mean(axis=0), ref.mean(axis=0)
        u, _, vt = np.linalg.svd((mobile - center).T @ (ref - target))
        rotation = u @ np.diag([1, 1, np.linalg.det(u @ vt)]) @ vt
        return (xyz - center) @ rotation + target

    def coordinates(self, index):
        if type(index) is not int or not 0 <= index < len(self.indices):
            raise ValueError('Frame index out of range')
        xyz = self._coordinates(self.indices[index])
        if self.reference is not None:
            xyz = self._align(xyz, xyz[self.align], self.reference)
        return xyz

    def read(self, index):
        frame = np.asarray((self.coordinates(index) * 10).T, dtype='<f4').tobytes()
        self.check_unchanged()
        return frame

    def _pdb_text(self, xyz, lengths, angles):
        # Preserve residue/atom names and serials; filter CONECT with the same map.
        records, atom_index, serials = [], 0, set()
        last_retained = None
        selected = dict(zip(self.keep, xyz * 10, strict=True))
        for line in self.original.splitlines(keepends=True):
            if line.startswith('ENDMDL'):
                break
            if line.startswith(('MODEL ', 'END', 'CRYST1')):
                continue
            if line.startswith(('ATOM  ', 'HETATM')):
                if atom_index in selected:
                    coords = selected[atom_index]
                    records.append(line[:30] + ''.join(f'{v:8.3f}' for v in coords) + line[54:].rstrip('\r\n') + '\n')
                    serials.add(line[6:11].strip())
                    last_retained = line
                atom_index += 1
            elif line.startswith('TER'):
                if last_retained is not None:
                    records.append('TER   ' + line[6:11] + '      ' + last_retained[17:27] + '\n')
                last_retained = None
            elif not line.startswith('CONECT'):
                records.append(line)
        for line in self.original.splitlines():
            if line.startswith('CONECT'):
                parts = [line[i:i+5].strip() for i in range(6, len(line), 5)]
                if parts and parts[0] in serials:
                    retained = [p for p in parts[1:] if p in serials]
                    if retained:
                        records.append('CONECT' + ''.join(f'{p:>5}' for p in [parts[0], *retained]) + '\n')
        if lengths is not None and not self.fit:
            a, b, c = lengths[0] * 10
            alpha, beta, gamma = angles[0]
            records.insert(0, f'CRYST1{a:9.3f}{b:9.3f}{c:9.3f}{alpha:7.2f}{beta:7.2f}{gamma:7.2f} P 1           1\n')
        return ''.join(records) + 'END\n'

    def close(self):
        pass  # Each read owns and closes its DCD handle.
