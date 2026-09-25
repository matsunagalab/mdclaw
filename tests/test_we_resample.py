"""Weighted-ensemble resampling and the CV evaluators, without OpenMM."""

import math

import numpy as np
import pytest

from mdclaw.analyze.cv import (
    CVError,
    compile_cvs,
    evaluate_cvs,
    evaluate_cvs_on_frames,
    half_box_nm,
    load_topology,
    minimum_image_cvs,
    normalize_cv_specs,
)
from mdclaw.we.resample import (
    ResampleError,
    assign_bin,
    bin_shape,
    in_target,
    normalize_edges,
    normalize_target,
    resample,
)


def _walkers(weights, pcoords):
    return [{"id": f"w{i}", "replica": i + 1, "weight": w, "pcoord": [p] if np.isscalar(p) else list(p)}
            for i, (w, p) in enumerate(zip(weights, pcoords))]


# ---------------------------------------------------------------------------
# bins and targets
# ---------------------------------------------------------------------------


class TestBins:
    def test_extended_edges_cover_the_line(self):
        edges = normalize_edges([[0.5, 1.0]], 1)
        assert edges[0].tolist() == [-np.inf, 0.5, 1.0, np.inf]
        assert bin_shape(edges) == (3,)
        assert assign_bin(np.array([0.2]), edges) == (0,)
        assert assign_bin(np.array([0.5]), edges) == (1,)
        assert assign_bin(np.array([0.7]), edges) == (1,)
        assert assign_bin(np.array([5.0]), edges) == (2,)

    def test_unextended_edges_refuse_outside_values(self):
        edges = normalize_edges([[0.5, 1.0, 1.5]], 1, extend=False)
        assert bin_shape(edges) == (2,)
        with pytest.raises(ResampleError) as excinfo:
            assign_bin(np.array([0.2]), edges)
        assert excinfo.value.code == "we_pcoord_out_of_bins"
        with pytest.raises(ResampleError):
            normalize_edges([[0.5]], 1, extend=False)

    @pytest.mark.parametrize("edges", [[[1.0, 0.5]], [[0.5, 0.5]], [[]], [[0.5], [0.5]], "x", [["a"]]])
    def test_invalid_edges(self, edges):
        with pytest.raises(ResampleError) as excinfo:
            normalize_edges(edges, 1)
        assert excinfo.value.code == "we_policy_args_invalid"

    def test_two_dimensional_bins(self):
        edges = normalize_edges([[0.5], [1.0, 2.0]], 2)
        assert bin_shape(edges) == (2, 3)
        assert assign_bin(np.array([0.7, 1.5]), edges) == (1, 1)

    def test_targets(self):
        target = normalize_target({"pcoord_ranges": [[0.8, None]]}, 1)
        assert target == [(0.8, np.inf)]
        assert in_target(np.array([0.9]), target) and not in_target(np.array([0.7]), target)
        assert not in_target(np.array([0.9]), None)
        two = normalize_target({"pcoord_ranges": [None, [0.0, 0.2]]}, 2)
        assert two[0] is None and in_target(np.array([5.0, 0.1]), two)
        for bad in ({"pcoord_ranges": [None]}, {"pcoord_ranges": [[1.0, 0.5]]}, {"x": 1}, {"pcoord_ranges": [[1]]}):
            with pytest.raises(ResampleError):
                normalize_target(bad, 1)


# ---------------------------------------------------------------------------
# resampling
# ---------------------------------------------------------------------------


class TestResample:
    EDGES = normalize_edges([[0.5, 1.5]], 1)

    def test_merges_bring_a_crowded_bin_down_and_conserve_weight(self):
        weights = np.array([0.30, 0.25, 0.20, 0.15, 0.05, 0.03, 0.02])
        walkers = _walkers(weights, [1.0] * 7)
        step = resample(walkers, walkers_per_bin=5, edges=self.EDGES, target=None, recycle=False,
                        basis_node_ids=[], seed=1)
        assert step["n_in"] == 7 and step["n_out"] == 5
        assert sum(c["weight"] for c in step["children"]) == pytest.approx(1.0)
        assert abs(step["weight_residual"]) < 1e-12
        fates = [w["fate"] for w in step["walkers"]]
        assert fates.count("merged") == 2 and fates.count("split") == 0
        merged = [w for w in step["walkers"] if w["fate"] == "merged"]
        assert all(w["merged_into"] in {x["id"] for x in step["walkers"]} for w in merged)
        assert all(w["children"] == [] for w in merged)
        assert step["bins"] == [{"bin": 1, "bin_indices": [1], "weight": pytest.approx(1.0), "n_in": 7, "n_out": 5}]
        assert {c["replica"] for c in step["children"]} == {1, 2, 3, 4, 5}

    def test_a_lone_walker_is_split_evenly(self):
        step = resample(_walkers([1.0], [1.0]), walkers_per_bin=5, edges=self.EDGES, target=None,
                        recycle=False, basis_node_ids=[], seed=3)
        assert step["n_out"] == 5
        assert [c["weight"] for c in step["children"]] == pytest.approx([0.2] * 5)
        assert all(c["parent_node_id"] == "w0" for c in step["children"])
        assert step["walkers"][0]["fate"] == "split" and step["walkers"][0]["children"] == [1, 2, 3, 4, 5]

    def test_heaviest_walker_is_split_first(self):
        walkers = _walkers([0.7, 0.2, 0.1], [1.0, 1.0, 1.0])
        step = resample(walkers, walkers_per_bin=5, edges=self.EDGES, target=None, recycle=False,
                        basis_node_ids=[], seed=0)
        by_parent = {}
        for child in step["children"]:
            by_parent.setdefault(child["parent_node_id"], []).append(child["weight"])
        assert len(by_parent["w0"]) == 3 and by_parent["w0"] == pytest.approx([0.7 / 3] * 3)
        assert by_parent["w1"] == pytest.approx([0.2]) and by_parent["w2"] == pytest.approx([0.1])

    def test_recycling_moves_target_weight_to_the_basis(self):
        walkers = _walkers([0.5, 0.3, 0.2], [1.0, 1.0, 2.0])
        target = normalize_target({"pcoord_ranges": [[1.8, None]]}, 1)
        step = resample(walkers, walkers_per_bin=2, edges=self.EDGES, target=target, recycle=True,
                        basis_node_ids=["eq_001"], seed=0)
        recycled = [w for w in step["walkers"] if w["fate"] == "recycled"]
        assert [w["id"] for w in recycled] == ["w2"]
        assert step["flux"] == {"weight_recycled": pytest.approx(0.2), "events": 1}
        assert step["target_weight"] == pytest.approx(0.2)
        child = [c for c in step["children"] if c.get("start_node_id")]
        assert len(child) == 1 and child[0]["start_node_id"] == "eq_001"
        assert child[0]["weight"] == pytest.approx(0.2) and child[0]["extra"] == {"recycled_from": "w2"}
        assert recycled[0]["bin"] is None
        assert sum(c["weight"] for c in step["children"]) == pytest.approx(1.0)

        # without recycling the same walker is simply binned (alone in its
        # bin, so it is split up to walkers_per_bin) and the target weight is
        # still reported
        kept = resample(walkers, walkers_per_bin=2, edges=self.EDGES, target=target, recycle=False,
                        basis_node_ids=[], seed=0)
        assert kept["flux"]["events"] == 0 and kept["target_weight"] == pytest.approx(0.2)
        w2 = next(w for w in kept["walkers"] if w["id"] == "w2")
        assert w2["fate"] == "split" and w2["bin"] == 2 and w2["in_target"] is True
        assert not any(c.get("start_node_id") for c in kept["children"])

    def test_same_seed_same_plan(self):
        walkers = _walkers(np.full(6, 1 / 6), [1.0] * 6)
        a = resample(walkers, walkers_per_bin=3, edges=self.EDGES, target=None, recycle=False,
                     basis_node_ids=[], seed=5)
        b = resample(walkers, walkers_per_bin=3, edges=self.EDGES, target=None, recycle=False,
                     basis_node_ids=[], seed=5)
        assert a["children"] == b["children"]

    def test_two_dimensional_walkers(self):
        edges = normalize_edges([[0.5], [0.5]], 2)
        walkers = _walkers([0.25] * 4, [(0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)])
        step = resample(walkers, walkers_per_bin=1, edges=edges, target=None, recycle=False,
                        basis_node_ids=[], seed=0)
        assert step["bin_shape"] == [2, 2]
        assert sorted(w["bin_indices"] for w in step["walkers"]) == [[0, 0], [0, 1], [1, 0], [1, 1]]
        assert step["n_out"] == 4

    @pytest.mark.parametrize("weights, code", [
        ([0.5, 0.4], "we_weights_invalid"),
        ([0.5, None], "we_weights_invalid"),
        ([0.5, -0.5], "we_weights_invalid"),
    ])
    def test_bad_weights_are_refused(self, weights, code):
        with pytest.raises(ResampleError) as excinfo:
            resample(_walkers(weights, [1.0, 1.0]), walkers_per_bin=2, edges=self.EDGES, target=None,
                     recycle=False, basis_node_ids=[], seed=0)
        assert excinfo.value.code == code

    def test_recycling_needs_target_and_basis(self):
        with pytest.raises(ResampleError) as excinfo:
            resample(_walkers([1.0], [1.0]), walkers_per_bin=2, edges=self.EDGES, target=None,
                     recycle=True, basis_node_ids=["eq_001"], seed=0)
        assert excinfo.value.code == "we_policy_args_invalid"
        with pytest.raises(ResampleError):
            resample(_walkers([1.0], [1.0]), walkers_per_bin=0, edges=self.EDGES, target=None,
                     recycle=False, basis_node_ids=[], seed=0)


# ---------------------------------------------------------------------------
# collective variables on synthetic trajectories
# ---------------------------------------------------------------------------


def _toy(tmp_path, frames, *, box=2.0, with_water=False):
    """A six-CA chain (one molecule), a one-atom ligand and optionally a water,
    written as topology.pdb + trajectory.dcd; returns (pdb, dcd, topology)."""
    import mdtraj as md

    top = md.Topology()
    chain = top.add_chain()
    previous = None
    for i in range(6):
        # a non-standard residue name: mdtraj then writes the CA-CA bonds as
        # CONECT records, so the bonds (hence the molecule) survive the PDB
        res = top.add_residue("BEA", chain)
        atom = top.add_atom("CA", md.element.carbon, res)
        if previous is not None:
            top.add_bond(previous, atom)
        previous = atom
    lig = top.add_residue("LIG", top.add_chain())
    top.add_atom("C1", md.element.carbon, lig)
    if with_water:
        wat = top.add_residue("HOH", top.add_chain())
        top.add_atom("O", md.element.oxygen, wat)
    xyz = np.asarray(frames, dtype=np.float32)
    n = xyz.shape[0]
    traj = md.Trajectory(xyz, top, unitcell_lengths=np.full((n, 3), box, dtype=np.float32),
                         unitcell_angles=np.full((n, 3), 90.0, dtype=np.float32))
    pdb = tmp_path / "topology.pdb"
    dcd = tmp_path / "trajectory.dcd"
    traj[0].save_pdb(str(pdb))
    traj.save_dcd(str(dcd))
    return pdb, dcd, traj


def _chain_frame(spacing, ligand=(1.9, 0.0, 0.0), water=None):
    # a slight zigzag keeps the chain off a line, so superposition is defined
    atoms = [(spacing * i, 0.05 * (i % 2), 0.0) for i in range(6)] + [ligand]
    if water is not None:
        atoms.append(water)
    return atoms


class TestCVs:
    def test_distance_raw_inside_a_molecule_and_minimum_image_between_molecules(self, tmp_path):
        pdb, dcd, _ = _toy(tmp_path, [_chain_frame(0.38)])
        topology = load_topology(pdb)
        specs = normalize_cv_specs([
            {"type": "distance", "name": "e2e", "selection_group1": "resid 0", "selection_group2": "resid 5"},
            {"type": "distance", "name": "lig", "selection_group1": "resname LIG", "selection_group2": "resid 0"},
        ])
        compiled = compile_cvs(specs, topology)
        assert minimum_image_cvs(compiled) == ["lig"]
        values = evaluate_cvs(dcd, topology, compiled)
        assert values.shape == (1, 2)
        # raw: 5 x 0.38 along x (plus the 0.05 zigzag), not folded to 0.1
        assert values[0, 0] == pytest.approx(math.hypot(1.9, 0.05), abs=1e-5)
        assert values[0, 1] == pytest.approx(0.1, abs=1e-5)     # minimum image in a 2 nm box
        assert half_box_nm(dcd) == pytest.approx(1.0)

    def test_distance_without_a_box_needs_no_box_inside_a_molecule(self, tmp_path):
        import mdtraj as md

        pdb, dcd, traj = _toy(tmp_path, [_chain_frame(0.38)])
        bare = md.Trajectory(traj.xyz, traj.topology)
        topology = load_topology(pdb)
        inner, outer = compile_cvs(normalize_cv_specs([
            {"type": "distance", "name": "e2e", "selection_group1": "resid 0", "selection_group2": "resid 5"},
            {"type": "distance", "name": "lig", "selection_group1": "resname LIG", "selection_group2": "resid 0"},
        ]), topology)
        assert evaluate_cvs_on_frames(bare, [inner])[0, 0] == pytest.approx(math.hypot(1.9, 0.05), abs=1e-5)
        with pytest.raises(CVError) as excinfo:
            evaluate_cvs_on_frames(bare, [outer])
        assert excinfo.value.code == "cv_box_missing"

    def test_q_and_rmsd_against_a_native_structure(self, tmp_path):
        import mdtraj as md

        native_dir = tmp_path / "native"
        native_dir.mkdir()
        native_pdb, _, _ = _toy(native_dir, [_chain_frame(0.2)])
        pdb, dcd, _ = _toy(tmp_path, [_chain_frame(0.2), _chain_frame(0.5)])
        topology = load_topology(pdb)
        specs = normalize_cv_specs([
            {"type": "q", "name": "q", "native_pdb": str(native_pdb), "selection": "name CA",
             "native_cutoff_nm": 1.0, "min_resid_gap": 2},
            {"type": "rmsd", "name": "r", "selection": "name CA", "reference_pdb": str(native_pdb)},
            {"type": "rmsd", "name": "r_aligned", "selection": "resid 3 to 5", "reference_pdb": str(native_pdb),
             "align_selection": "resid 0 to 2"},
        ])
        compiled = compile_cvs(specs, topology)
        assert compiled[0].n_contacts == 5
        values = evaluate_cvs(dcd, topology, compiled)
        assert values[0, 0] == pytest.approx(1.0, abs=1e-3) and values[1, 0] < 0.05
        assert values[0, 1] == pytest.approx(0.0, abs=1e-4) and values[1, 1] > 0.3
        reference = md.load(str(native_pdb))
        traj = md.load(str(dcd), top=str(pdb))
        expected = md.rmsd(traj, reference, atom_indices=topology.select("name CA"))
        assert values[:, 1] == pytest.approx(expected, abs=1e-4)
        assert values[0, 2] == pytest.approx(0.0, abs=1e-4) and values[1, 2] > 0.3

    def test_dihedral_in_degrees(self, tmp_path):
        cis = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 2, 0), (3, 3, 0), (1.9, 0, 0)]
        trans = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (2, 1, 0), (2, 2, 0), (3, 3, 0), (1.9, 0, 0)]
        pdb, dcd, _ = _toy(tmp_path, [cis, trans], box=10.0)
        topology = load_topology(pdb)
        compiled = compile_cvs(normalize_cv_specs([
            {"type": "dihedral", "name": "phi", "atoms": [0, 1, 2, 3]},
            {"type": "dihedral", "name": "phi_sel", "selections": ["resid 0", "resid 1", "resid 2", "resid 3"]},
        ]), topology)
        values = evaluate_cvs(dcd, topology, compiled)
        assert values[0, 0] == pytest.approx(0.0, abs=1e-3)
        assert abs(values[1, 0]) == pytest.approx(180.0, abs=1e-3)
        assert values[:, 1] == pytest.approx(values[:, 0], abs=1e-6)

    def test_spec_and_selection_errors(self, tmp_path):
        pdb, _, _ = _toy(tmp_path, [_chain_frame(0.38, water=(0.5, 0.5, 0.5))], with_water=True)
        topology = load_topology(pdb)
        for bad in (
            [],
            [{"type": "angle", "name": "a"}],
            [{"type": "distance", "name": "1bad", "selection_group1": "resid 0", "selection_group2": "resid 1"}],
            [{"type": "distance", "name": "d", "selection_group1": "resid 0"}],
            [{"type": "distance", "name": "d", "selection_group1": "resid 0", "selection_group2": "resid 1", "x": 1}],
            [{"type": "distance", "name": "d", "selection_group1": "resid 0", "selection_group2": "resid 1"},
             {"type": "distance", "name": "d", "selection_group1": "resid 0", "selection_group2": "resid 2"}],
            [{"type": "rmsd", "name": "r", "reference_pdb": "/no/such/file.pdb"}],
            [{"type": "dihedral", "name": "p", "atoms": [0, 1, 2]}],
            [{"type": "dihedral", "name": "p", "atoms": [0, 1, 2, 3], "selections": ["a", "b", "c", "d"]}],
            [{"type": "q", "name": "q", "native_pdb": str(pdb), "min_resid_gap": -1}],
        ):
            with pytest.raises(CVError) as excinfo:
                normalize_cv_specs(bad)
            assert excinfo.value.code == "cv_spec_invalid"
        for spec in (
            {"type": "distance", "name": "d", "selection_group1": "resid 0", "selection_group2": "resid 0"},
            {"type": "distance", "name": "d", "selection_group1": "resname HOH", "selection_group2": "resid 0"},
            {"type": "distance", "name": "d", "selection_group1": "resname XYZ", "selection_group2": "resid 0"},
            {"type": "distance", "name": "d", "selection_group1": "resid (", "selection_group2": "resid 0"},
            {"type": "dihedral", "name": "p", "selections": ["resid 0 to 1", "resid 2", "resid 3", "resid 4"]},
            {"type": "dihedral", "name": "p", "atoms": [0, 1, 2, 99]},
        ):
            with pytest.raises(CVError) as excinfo:
                compile_cvs(normalize_cv_specs([spec]), topology)
            assert excinfo.value.code == "cv_selection_invalid"
