"""Unit tests for the ``fep`` server: protocol, mapping, tripeptide, MBAR.

The pure-Python parts (protocol schedule, atom mapping on synthetic records,
tripeptide extraction, MBAR on a harmonic-oscillator toy) run everywhere.
``TestHybridVacuum`` builds a real ACE-X-NME hybrid System with amber14 on the
Reference platform and is marked ``slow``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from mdclaw.fep import TOOLS
from mdclaw.fep.analysis import FepAnalysisError, analyze_fep, collect_windows, estimate_ddg, run_mbar
from mdclaw.fep.mapping import AtomRecord, MappingError, map_mutation
from mdclaw.fep.protocol import (
    DEFAULT_N_WINDOWS,
    PHASE_BOUNDS,
    ProtocolError,
    build_protocol,
    lambda_to_parameters,
    load_protocol,
    parse_lambda_indices,
    protocols_equivalent,
    windows_from_schedule,
)
from mdclaw.fep.tripeptide import extract_tripeptide
from mdclaw.fep.hybrid import FEP_PARAMETERS, STATE_A, STATE_B


# --------------------------------------------------------------------------- #
# registry                                                                      #
# --------------------------------------------------------------------------- #

def test_fep_tools_registered():
    assert set(TOOLS) == {"build_hybrid_system", "run_fep", "analyze_fep", "extract_tripeptide", "estimate_ddg"}


# --------------------------------------------------------------------------- #
# protocol                                                                      #
# --------------------------------------------------------------------------- #

class TestProtocol:
    def test_endpoints_match_states(self):
        assert lambda_to_parameters(0.0) == STATE_A
        assert lambda_to_parameters(1.0) == STATE_B
        assert set(STATE_A) == set(FEP_PARAMETERS)

    def test_phases_are_sequential(self):
        p1, p2 = PHASE_BOUNDS
        mid_decharge = lambda_to_parameters(p1 / 2)
        assert 0 < mid_decharge["fep_elec_old"] < 1
        assert mid_decharge["fep_sterics_old"] == 1.0 and mid_decharge["fep_elec_new"] == 0.0
        mid_swap = lambda_to_parameters((p1 + p2) / 2)
        assert mid_swap["fep_elec_old"] == 0.0 and mid_swap["fep_elec_new"] == 0.0
        assert math.isclose(mid_swap["fep_sterics_old"] + mid_swap["fep_sterics_new"], 1.0)
        assert math.isclose(mid_swap["fep_core"], mid_swap["fep_sterics_new"])

    def test_default_windows(self):
        windows = windows_from_schedule(None)
        assert len(windows) == DEFAULT_N_WINDOWS
        assert windows[0]["parameters"] == STATE_A
        assert windows[-1]["parameters"] == STATE_B
        assert [w["index"] for w in windows] == list(range(DEFAULT_N_WINDOWS))

    def test_schedule_from_csv_and_json(self):
        csv = windows_from_schedule("0, 0.5, 1")
        js = windows_from_schedule("[0, 0.5, 1]")
        assert [w["lambda"] for w in csv] == [w["lambda"] for w in js] == [0.0, 0.5, 1.0]

    @pytest.mark.parametrize("bad", [
        "[0.2, 1]", "[0, 0.7]", "[0]", "[0, 1.5]",
        "0,0.5,0.25,1",          # non-monotonic: index order must be lambda order
        "0,0.5,0.5,1",           # duplicate window
        "0,a,1",                 # not a number
        '[{"fep_core": 0}]',     # parameter dicts are no longer accepted
    ])
    def test_schedule_rejects_bad_input(self, bad):
        with pytest.raises(ProtocolError) as exc:
            windows_from_schedule(bad)
        assert exc.value.code == "fep_protocol_invalid"

    def test_protocols_equivalent(self):
        a = build_protocol(mutation={"label": "A:L99A"}, windows=windows_from_schedule(None, n_windows=5))
        b = build_protocol(mutation={"label": "A:L99A"}, windows=windows_from_schedule("0,0.25,0.5,0.75,1"))
        c = build_protocol(mutation={"label": "A:L99A"}, windows=windows_from_schedule(None, n_windows=6))
        assert protocols_equivalent(a, b)
        assert not protocols_equivalent(a, c)
        assert not protocols_equivalent(a, {**b, "mutation": {"label": "A:L99G"}})

    def test_n_windows_minimum(self):
        with pytest.raises(ProtocolError):
            windows_from_schedule(None, n_windows=2)

    def test_roundtrip(self, tmp_path):
        protocol = build_protocol(mutation={"label": "A:L99A"}, windows=windows_from_schedule(None, n_windows=5))
        path = tmp_path / "fep_protocol.json"
        path.write_text(json.dumps(protocol))
        loaded = load_protocol(path)
        assert loaded["n_windows"] == 5 and loaded["mutation"]["label"] == "A:L99A"

    def test_parse_lambda_indices(self):
        assert parse_lambda_indices(None, 5) == [0, 1, 2, 3, 4]
        assert parse_lambda_indices("all", 5) == [0, 1, 2, 3, 4]
        assert parse_lambda_indices("0-2,4", 5) == [0, 1, 2, 4]
        assert parse_lambda_indices([3, 1, 1], 5) == [1, 3]
        with pytest.raises(ProtocolError) as exc:
            parse_lambda_indices("0-7", 5)
        assert exc.value.code == "fep_lambda_index_invalid"

    @pytest.mark.parametrize("bad", ["-1", "a-b", "0-2,x", "1.5", "", "3-1"])
    def test_parse_lambda_indices_rejects_garbage(self, bad):
        if bad == "":
            assert parse_lambda_indices(bad, 5) == [0, 1, 2, 3, 4]  # empty means all
            return
        with pytest.raises(ProtocolError) as exc:
            parse_lambda_indices(bad, 5)
        assert exc.value.code == "fep_lambda_index_invalid"


# --------------------------------------------------------------------------- #
# mapping (synthetic records)                                                   #
# --------------------------------------------------------------------------- #

def _records(residues: list[tuple[str, list[str]]]) -> list[AtomRecord]:
    out, idx = [], 0
    for r_i, (resname, names) in enumerate(residues):
        for n in names:
            out.append(AtomRecord(index=idx, name=n, element=n[0], residue_index=r_i,
                                  residue_name=resname, residue_id=str(r_i + 1), chain_id="A"))
            idx += 1
    return out


BACKBONE = ["N", "H", "CA", "HA", "C", "O"]
ALA = BACKBONE + ["CB", "HB1", "HB2", "HB3"]
LEU = BACKBONE + ["CB", "HB2", "HB3", "CG", "HG", "CD1", "HD11", "HD12", "HD13", "CD2", "HD21", "HD22", "HD23"]
WAT = ["O", "H1", "H2"]


class TestMapping:
    def test_leu_to_ala_core_and_dummies(self):
        old = _records([("ALA", ALA), ("LEU", LEU), ("WAT", WAT)])
        new = _records([("ALA", ALA), ("ALA", ALA), ("WAT", WAT)])
        m = map_mutation(old, new, 1)
        core = {(old[a].name, new[b].name) for a, b in m.core_pairs}
        # backbone + CB are shared; HB1 has no counterpart in LEU (HB2/HB3 do).
        assert {("N", "N"), ("CA", "CA"), ("C", "C"), ("O", "O"), ("CB", "CB")} <= core
        assert {old[a].name for a in m.unique_old} >= {"CG", "CD1", "CD2", "HG"}
        assert {new[b].name for b in m.unique_new} >= {"HB1"}
        # every atom of both states lands in the hybrid, environment shared
        assert set(m.old_to_hybrid) == set(range(len(old)))
        assert set(m.new_to_hybrid) == set(range(len(new)))
        assert m.n_hybrid == len(old) + len(m.unique_new)
        assert m.old_to_hybrid[len(old) - 1] == m.new_to_hybrid[len(new) - 1]  # water O
        j = m.to_json()
        assert j["residue"]["old_name"] == "LEU" and j["residue"]["new_name"] == "ALA"

    def test_environment_mismatch_is_rejected(self):
        old = _records([("ALA", ALA), ("LEU", LEU), ("WAT", WAT)])
        new = _records([("ALA", ALA), ("ALA", ALA)])  # water missing
        with pytest.raises(MappingError) as exc:
            map_mutation(old, new, 1)
        assert exc.value.code == "fep_environment_mismatch"

    def test_new_dummy_pdb_names_are_unique(self):
        old = _records([("GLY", ["N", "H", "CA", "HA2", "HA3", "C", "O"])])
        new = _records([("ALA", ALA)])
        m = map_mutation(old, new, 0)
        names = list(m.hybrid_new_pdb_names.values())
        assert len(names) == len(set(names))
        assert not (set(names) & {r.name for r in old})


# --------------------------------------------------------------------------- #
# tripeptide                                                                    #
# --------------------------------------------------------------------------- #

def _pdb_line(serial, name, resname, resseq, x, y, z, element):
    name_field = f" {name:<3}" if len(name) < 4 else name
    return (f"ATOM  {serial:5d} {name_field} {resname:>3} A{resseq:4d}    {x:8.3f}{y:8.3f}{z:8.3f}"
            f"  1.00  0.00          {element:>2}")


def _write_pentapeptide(path: Path, gap_after: int | None = None) -> Path:
    """GLY-ALA-LEU-ALA-GLY backbone with N/CA/C/O plus CB; residues 10..14."""
    lines, serial = [], 1
    for i, res in enumerate(["GLY", "ALA", "LEU", "ALA", "GLY"]):
        x0 = 3.8 * i + (5.0 if gap_after is not None and i > gap_after else 0.0)
        for name, dx, el in (("N", 0.0, "N"), ("CA", 1.4, "C"), ("C", 2.5, "C"), ("O", 2.6, "O")):
            lines.append(_pdb_line(serial, name, res, 10 + i, x0 + dx, 0.0 if name != "O" else 1.2, 0.0, el))
            serial += 1
        if res != "GLY":
            lines.append(_pdb_line(serial, "CB", res, 10 + i, x0 + 1.4, -1.5, 0.0, "C"))
            serial += 1
    lines += ["TER", "END", ""]
    path.write_text("\n".join(lines))
    return path


class TestTripeptide:
    def test_extracts_flanked_fragment(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        res = extract_tripeptide(str(pdb), "A:L12A")
        assert res["success"], res
        assert res["n_residues"] == 3
        assert res["residues"] == ["A:ALA11", "A:LEU12", "A:ALA13"]
        out = Path(res["tripeptide_pdb"]).read_text().splitlines()
        atoms = [ln for ln in out if ln.startswith("ATOM")]
        assert {ln[22:26].strip() for ln in atoms} == {"11", "12", "13"}
        assert [int(ln[6:11]) for ln in atoms] == list(range(1, len(atoms) + 1))
        assert not res["warnings"]

    def test_truncated_at_chain_end(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        res = extract_tripeptide(str(pdb), "A:G10A", flank=2)
        assert res["success"]
        assert res["n_residues"] == 3  # 10, 11, 12
        assert any("could be kept" in w for w in res["warnings"])

    def test_chain_break_is_flagged(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb", gap_after=1)
        res = extract_tripeptide(str(pdb), "A:L12A")
        assert res["success"]
        assert any("chain break" in w for w in res["warnings"])

    def test_bad_mutation(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        res = extract_tripeptide(str(pdb), "A:L99A")
        assert res["success"] is False
        assert res["code"] == "fep_mutation_residue_not_found"

    def test_missing_file(self, tmp_path):
        res = extract_tripeptide(str(tmp_path / "nope.pdb"), "A:L12A")
        assert res["code"] == "file_not_found"


# --------------------------------------------------------------------------- #
# MBAR on a harmonic toy                                                        #
# --------------------------------------------------------------------------- #

def _harmonic_windows(tmp_path: Path, n_states: int = 5, n_samples: int = 400, seed: int = 0,
                      *, split_nodes: bool = False) -> tuple[list[Path], Path, float]:
    """1-D harmonic oscillators U_k(x) = 0.5 * K_k * x^2 sampled exactly.

    Analytic reduced free energy f_k = -0.5 ln(2 pi / K_k), so
    dG(0 -> K-1) = kT * 0.5 * ln(K_last / K_first).
    """
    rng = np.random.default_rng(seed)
    ks = np.linspace(1.0, 4.0, n_states)
    kT = 0.008314462618 * 300.0
    protocol = build_protocol(mutation={"label": "A:L99A"}, windows=windows_from_schedule(None, n_windows=n_states))
    protocol_file = tmp_path / "fep_protocol.json"
    protocol_file.write_text(json.dumps(protocol))

    def _index(node_id: str, indices: list[int]) -> Path:
        node_dir = tmp_path / node_id
        node_dir.mkdir()
        windows = {}
        for k in indices:
            x = rng.normal(0.0, 1.0 / math.sqrt(ks[k]), size=n_samples)
            u_kn = 0.5 * ks[:, None] * x[None, :] ** 2  # already in kT units
            wdir = node_dir / f"window_{k:02d}"
            wdir.mkdir()
            np.savez_compressed(wdir / "energies.npz", u_kn=u_kn, time_ps=np.arange(n_samples, dtype=float))
            windows[str(k)] = {"index": k, "segments": [{"node_id": node_id, "energies_file": str(wdir / "energies.npz")}]}
        idx = {
            "schema_version": 1, "node_id": node_id, "fep_protocol_file": str(protocol_file),
            "n_protocol_windows": n_states, "lambda_indices": indices, "temperature_kelvin": 300.0,
            "pressure_bar": None, "windows": windows,
        }
        p = node_dir / "fep_windows.json"
        p.write_text(json.dumps(idx))
        return p

    if split_nodes:
        files = [_index("fep_001", list(range(0, 2))), _index("fep_002", list(range(2, n_states)))]
    else:
        files = [_index("fep_001", list(range(n_states)))]
    expected = kT * 0.5 * math.log(ks[-1] / ks[0])
    return files, protocol_file, expected


class TestAnalysis:
    def test_run_mbar_recovers_analytic_dg(self, tmp_path):
        pytest.importorskip("pymbar")
        files, protocol_file, expected = _harmonic_windows(tmp_path)
        collected = collect_windows([str(f) for f in files])
        res = run_mbar(collected["windows"], load_protocol(protocol_file), 300.0, discard_fraction=0.0, subsample=False)
        assert abs(res["dG_kj_mol"] - expected) < 4 * res["dG_error_kj_mol"] + 0.05
        assert res["n_states"] == 5 and all(n == 400 for n in res["n_samples_per_state"])
        assert len(res["neighbour_overlap"]) == 4
        assert set(res["phases"]) == {"decharge_old_kj_mol", "sterics_swap_kj_mol", "recharge_new_kj_mol"}
        assert res["cumulative_dG_kj_mol"][0] == 0.0

    def test_windows_split_across_nodes_are_merged(self, tmp_path):
        pytest.importorskip("pymbar")
        files, _, expected = _harmonic_windows(tmp_path, split_nodes=True)
        res = analyze_fep(fep_windows_files=[str(f) for f in files], output_dir=str(tmp_path / "out"),
                          discard_fraction=0.0, subsample=False)
        assert res["success"], res
        assert abs(res["dG_kj_mol"] - expected) < 4 * res["dG_error_kj_mol"] + 0.05
        report = json.loads(Path(res["fep_result"]).read_text())
        assert report["mutation"]["label"] == "A:L99A"
        assert len(report["sources"]) == 2

    def test_incomplete_windows_fail_with_indices(self, tmp_path):
        pytest.importorskip("pymbar")
        files, _, _ = _harmonic_windows(tmp_path, split_nodes=True)
        res = analyze_fep(fep_windows_files=[str(files[0])], output_dir=str(tmp_path / "out"))
        assert res["success"] is False
        assert res["code"] == "fep_windows_incomplete"
        assert "[2, 3, 4]" in res["errors"][0]

    def test_missing_inputs_code(self, tmp_path):
        res = analyze_fep(output_dir=str(tmp_path))
        assert res["code"] == "fep_windows_missing"

    def test_incompatible_protocols(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        files_a, _, _ = _harmonic_windows(tmp_path / "a")
        files_b, _, _ = _harmonic_windows(tmp_path / "b", n_states=7)
        with pytest.raises(FepAnalysisError) as exc:
            collect_windows([str(files_a[0]), str(files_b[0])])
        assert exc.value.code == "fep_windows_incompatible"

    def test_estimate_ddg(self, tmp_path):
        folded = tmp_path / "folded.json"
        unfolded = tmp_path / "unfolded.json"
        folded.write_text(json.dumps({"mutation": {"label": "A:L99A"}, "dG_kj_mol": 10.0, "dG_error_kj_mol": 0.3,
                                      "warnings": ["low overlap 3-4"]}))
        unfolded.write_text(json.dumps({"mutation": {"label": "A:L2A"}, "dG_kj_mol": 4.0, "dG_error_kj_mol": 0.4}))
        res = estimate_ddg(str(folded), str(unfolded), output_file=str(tmp_path / "out" / "ddg.json"))
        assert res["success"], res
        assert math.isclose(res["ddG_kj_mol"], 6.0)
        assert math.isclose(res["ddG_error_kj_mol"], 0.5)
        assert math.isclose(res["ddG_kcal_mol"], 6.0 / 4.184)
        assert any("[folded] low overlap" in w for w in res["warnings"])
        assert Path(res["ddg_file"]) == tmp_path / "out" / "ddg.json"
        # never inside the (immutable) analyze node's artifacts: default goes to the study's evidence/
        study = tmp_path / "study"
        (study / "jobs").mkdir(parents=True)
        res2 = estimate_ddg(str(folded), str(unfolded), study_dir=str(study))
        assert Path(res2["ddg_file"]).parent == study / "evidence"
        assert not (tmp_path / "ddg.json").exists()

    def test_estimate_ddg_rejects_non_result(self, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text("{}")
        res = estimate_ddg(str(bad), str(bad))
        assert res["success"] is False and res["code"] == "fep_result_invalid"


# --------------------------------------------------------------------------- #
# DAG wiring                                                                    #
# --------------------------------------------------------------------------- #

class TestDag:
    def test_fep_node_parents(self, tmp_path):
        from mdclaw._node import create_node, init_progress_v3, validate_node_execution_context
        from tests.pipeline_helpers import complete_node_with_placeholders as complete

        jd = tmp_path / "job"
        jd.mkdir()
        init_progress_v3(str(jd))
        eq = create_node(str(jd), "eq")["node_id"]
        complete(str(jd), eq, {"state": "artifacts/equilibrated.xml"})
        fep1 = create_node(str(jd), "fep", parent_node_ids=[eq])
        assert fep1["success"], fep1
        complete(str(jd), fep1["node_id"], {"fep_windows": "artifacts/fep_windows.json"})
        fep2 = create_node(str(jd), "fep", parent_node_ids=[fep1["node_id"]])
        assert fep2["success"], fep2
        # fep -> analyze uses the alchemical scope (there is no production chain)
        an = create_node(str(jd), "analyze", parent_node_ids=[fep1["node_id"]],
                         conditions={"analysis_data_scope": "alchemical"})
        assert an["success"], an
        wrong = create_node(str(jd), "analyze", parent_node_ids=[fep1["node_id"]],
                            conditions={"analysis_data_scope": "production_chain"})
        assert wrong["success"] is False and wrong["code"] == "analyze_conditions_invalid"
        # prod parents cannot be mixed with fep parents
        prod = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
        mixed = create_node(str(jd), "analyze", parent_node_ids=[fep1["node_id"], prod],
                            conditions={"analysis_data_scope": "alchemical"})
        assert mixed["success"] is False and mixed["code"] == "analyze_parents_mixed"
        not_fep = create_node(str(jd), "analyze", parent_node_ids=[prod],
                              conditions={"analysis_data_scope": "alchemical"})
        assert not_fep["success"] is False and not_fep["code"] == "analyze_conditions_invalid"
        # fep cannot run off a prod parent (parent types are checked at execution)
        complete(str(jd), prod, {"state": "artifacts/final.xml"})
        bad = create_node(str(jd), "fep", parent_node_ids=[prod])["node_id"]
        ctx = validate_node_execution_context(str(jd), bad, "fep")
        assert ctx["success"] is False
        assert "parent_type_invalid" in ctx["blocking_codes"]
        ok = validate_node_execution_context(str(jd), fep2["node_id"], "fep")
        assert ok["success"], ok


# --------------------------------------------------------------------------- #
# real hybrid System (slow)                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
class TestHybridVacuum:
    @staticmethod
    def _peptide(resname: str, tmp_path: Path):
        pdbfixer = pytest.importorskip("pdbfixer")
        text = "\n".join([
            _pdb_line(1, "CH3", "ACE", 1, 2.0, 1.0, 0.0, "C"), _pdb_line(2, "C", "ACE", 1, 1.5, 2.4, 0.0, "C"),
            _pdb_line(3, "O", "ACE", 1, 0.3, 2.6, 0.0, "O"), _pdb_line(4, "N", resname, 2, 2.4, 3.4, 0.0, "N"),
            _pdb_line(5, "CA", resname, 2, 2.0, 4.8, 0.0, "C"), _pdb_line(6, "C", resname, 2, 3.2, 5.7, 0.0, "C"),
            _pdb_line(7, "O", resname, 2, 4.3, 5.3, 0.0, "O"), _pdb_line(8, "N", "NME", 3, 3.0, 7.0, 0.0, "N"),
            _pdb_line(9, "C", "NME", 3, 4.0, 8.0, 0.0, "C"), "END", "",
        ])
        path = tmp_path / f"pep_{resname}.pdb"
        path.write_text(text)
        fixer = pdbfixer.PDBFixer(filename=str(path))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()
        fixer.addMissingAtoms()
        fixer.addMissingHydrogens(7.0)
        return fixer.topology, fixer.positions

    @pytest.mark.parametrize("old,new", [
        ("LEU", "ALA"),   # large -> small
        ("GLY", "PRO"),   # ring closing onto the backbone N
        ("LEU", "PHE"),   # CG / CD1 / CD2 exist in both but never map (name clash)
        ("ALA", "TRP"),   # small -> large, ring appears
        ("VAL", "ILE"),   # single beta hydrogen HB stays core
    ])
    def test_endpoints_reproduce_end_states(self, tmp_path, old, new):
        openmm = pytest.importorskip("openmm")
        from openmm import app, unit

        from mdclaw.fep.hybrid import build_hybrid, relax_dummy_atoms, validate_endpoints
        from mdclaw.fep.build import locate_residue_index
        from mdclaw.fep.mapping import atom_records_from_topology, bonds_from_topology

        ff = app.ForceField("amber14-all.xml")
        top_a, pos_a = self._peptide(old, tmp_path)
        top_b, pos_b = self._peptide(new, tmp_path)
        sys_a = ff.createSystem(top_a, nonbondedMethod=app.NoCutoff, constraints=app.HBonds)
        sys_b = ff.createSystem(top_b, nonbondedMethod=app.NoCutoff, constraints=app.HBonds)
        for s, t, p in ((sys_a, top_a, pos_a), (sys_b, top_b, pos_b)):
            sim = app.Simulation(t, s, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
            sim.context.setPositions(p)
            sim.minimizeEnergy(maxIterations=200)
            p[:] = sim.context.getState(getPositions=True).getPositions()
        ra, rb = atom_records_from_topology(top_a), atom_records_from_topology(top_b)
        mapping = map_mutation(ra, rb, locate_residue_index(top_a, "A", "2"),
                               old_bonds=bonds_from_topology(top_a), new_bonds=bonds_from_topology(top_b))
        build = build_hybrid(sys_a, np.array(pos_a.value_in_unit(unit.nanometer)),
                             sys_b, np.array(pos_b.value_in_unit(unit.nanometer)), mapping)
        relax_dummy_atoms(build, platform_name="Reference")
        val = validate_endpoints(build, sys_a, sys_b, platform_name="Reference")
        assert val["passed"], val
        # the hybrid System must survive an XML round trip with its globals
        xml = openmm.XmlSerializer.serialize(build.system)
        assert all(name in xml for name in FEP_PARAMETERS)


# --------------------------------------------------------------------------- #
# end-to-end in vacuum: build_hybrid -> run_fep -> analyze_fep (slow)          #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
class TestVacuumPipeline:
    """The ``run_fep -> fep_windows.json -> analyze_fep`` contract on a
    30-atom ACE-LEU-NME -> ACE-ALA-NME hybrid on the Reference platform:
    relative index paths, partial-index recovery, per-segment chaining."""

    @staticmethod
    def _hybrid_triple(tmp_path: Path):
        openmm = pytest.importorskip("openmm")
        pytest.importorskip("pymbar")
        from openmm import app, unit

        from mdclaw.fep.build import hybrid_topology, locate_residue_index
        from mdclaw.fep.hybrid import build_hybrid, relax_dummy_atoms
        from mdclaw.fep.mapping import atom_records_from_topology, bonds_from_topology

        ff = app.ForceField("amber14-all.xml")
        top_a, pos_a = TestHybridVacuum._peptide("LEU", tmp_path)
        top_b, pos_b = TestHybridVacuum._peptide("ALA", tmp_path)
        sys_a = ff.createSystem(top_a, nonbondedMethod=app.NoCutoff, constraints=app.HBonds)
        sys_b = ff.createSystem(top_b, nonbondedMethod=app.NoCutoff, constraints=app.HBonds)
        for s, t, p in ((sys_a, top_a, pos_a), (sys_b, top_b, pos_b)):
            sim = app.Simulation(t, s, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
            sim.context.setPositions(p)
            sim.minimizeEnergy(maxIterations=200)
            p[:] = sim.context.getState(getPositions=True).getPositions()
        mapping = map_mutation(atom_records_from_topology(top_a), atom_records_from_topology(top_b),
                               locate_residue_index(top_a, "A", "2"),
                               old_bonds=bonds_from_topology(top_a), new_bonds=bonds_from_topology(top_b))
        build = build_hybrid(sys_a, np.array(pos_a.value_in_unit(unit.nanometer)),
                             sys_b, np.array(pos_b.value_in_unit(unit.nanometer)), mapping)
        relax_dummy_atoms(build, platform_name="Reference")
        topo = tmp_path / "topo"
        topo.mkdir()
        (topo / "system.xml").write_text(openmm.XmlSerializer.serialize(build.system))
        with (topo / "topology.pdb").open("w") as fh:
            app.PDBFile.writeFile(hybrid_topology(top_a, top_b, mapping), build.positions_nm * unit.nanometer, fh)
        protocol = build_protocol(mutation={"label": "A:L2A"}, windows=windows_from_schedule("0,0.5,1"))
        (topo / "fep_protocol.json").write_text(json.dumps(protocol))
        return topo

    def test_run_fep_to_analyze_fep(self, tmp_path, monkeypatch):
        from mdclaw.fep.run import load_windows_index, run_fep

        topo = self._hybrid_triple(tmp_path)
        common = dict(system_xml_file=str(topo / "system.xml"), topology_pdb_file=str(topo / "topology.pdb"),
                      fep_protocol_file=str(topo / "fep_protocol.json"), platform="Reference",
                      sampling_time_ns=0.002, equilibration_time_ns=0.0005, sample_interval_ps=0.1,
                      timestep_fs=1.0, hmr=False, random_seed=7)
        # bad arguments are rejected before anything runs, with structured codes
        bad = run_fep(**{**common, "sampling_time_ns": 0.0}, output_dir=str(tmp_path / "bad"))
        assert bad["success"] is False and bad["code"] == "invalid_parameter_value"
        bad = run_fep(**{**common, "lambda_indices": "a-b"}, output_dir=str(tmp_path / "bad"))
        assert bad["code"] == "fep_lambda_index_invalid"
        bad = run_fep(**{**common, "platform": "gpu"}, output_dir=str(tmp_path / "bad"))
        assert bad["code"] == "invalid_parameter_value"

        # first node: windows 0-1 only (as a job-array member would)
        first = run_fep(**common, lambda_indices="0-1", output_dir=str(tmp_path / "run"))
        assert first["success"], first
        assert first["lambda_indices"] == [0, 1] and first["ensemble"] == "NVT"
        index_file = Path(first["fep_windows"])
        raw = json.loads(index_file.read_text())
        assert raw["complete"] is True and raw["schema_version"] == 2
        # every path in the index is relative to the index file
        assert not Path(raw["fep_protocol_file"]).is_absolute()
        for rec in raw["windows"].values():
            assert not Path(rec["state_file"]).is_absolute()
            assert all(not Path(s["energies_file"]).is_absolute() for s in rec["segments"])
        assert rec["start_minimisation"]["after_kj_mol"] <= rec["start_minimisation"]["before_kj_mol"] + 1e-6

        # analysis refuses an incomplete protocol and names the missing windows
        partial = analyze_fep(fep_windows_files=[str(index_file)], output_dir=str(tmp_path / "an0"),
                              discard_fraction=0.0, subsample=False)
        assert partial["code"] == "fep_windows_incomplete" and "[2]" in partial["errors"][0]

        # second node: window 2, plus continue windows 0-1 from the first index
        # (the --restart-windows-file recovery path)
        second = run_fep(**common, lambda_indices="all", restart_windows_file=str(index_file),
                         output_dir=str(tmp_path / "run"))
        assert second["success"], second
        idx2 = load_windows_index(second["fep_windows"])
        assert sorted(idx2["windows"]) == [0, 1, 2]
        assert len(idx2["windows"][0]["segments"]) == 2 and len(idx2["windows"][2]["segments"]) == 1
        assert idx2["windows"][0]["start_minimisation"] is None  # continued, not restarted from eq
        assert Path(idx2["windows"][0]["segments"][0]["energies_file"]).is_file()

        res = analyze_fep(fep_windows_files=[second["fep_windows"]], output_dir=str(tmp_path / "an"),
                          discard_fraction=0.0, subsample=False)
        assert res["success"], res
        assert res["n_states"] == 3
        assert res["n_samples_per_state"] == [40, 40, 20]  # 2 + 1 segments of 20 samples
        assert math.isfinite(res["dG_kj_mol"]) and res["dG_error_kj_mol"] > 0
        report = json.loads(Path(res["fep_result"]).read_text())
        assert report["ensemble"] == "NVT" and report["per_window"][0]["n_segments"] == 2

        # relocating the whole tree keeps the index usable (no absolute paths)
        moved = tmp_path / "elsewhere"
        (tmp_path / "run").rename(moved)
        (tmp_path / "topo").rename(moved / "topo_moved")
        new_index = Path(second["fep_windows"]).as_posix().replace(str(tmp_path / "run"), str(moved))
        # the protocol moved too: point the index at it the way a moved job would (same relative layout)
        idx_raw = json.loads(Path(new_index).read_text())
        idx_raw["fep_protocol_file"] = "../topo_moved/fep_protocol.json"
        Path(new_index).write_text(json.dumps(idx_raw))
        again = analyze_fep(fep_windows_files=[new_index], output_dir=str(tmp_path / "an2"),
                            discard_fraction=0.0, subsample=False)
        assert again["success"], again
        assert math.isclose(again["dG_kj_mol"], res["dG_kj_mol"])

    def test_extension_pressure_mismatch_is_refused(self, tmp_path):
        from mdclaw.fep.run import run_fep

        topo = self._hybrid_triple(tmp_path)
        common = dict(system_xml_file=str(topo / "system.xml"), topology_pdb_file=str(topo / "topology.pdb"),
                      fep_protocol_file=str(topo / "fep_protocol.json"), platform="Reference",
                      sampling_time_ns=0.001, equilibration_time_ns=0.0, sample_interval_ps=0.1,
                      timestep_fs=1.0, hmr=False)
        first = run_fep(**common, lambda_indices="0", output_dir=str(tmp_path / "run"))
        assert first["success"], first
        idx = json.loads(Path(first["fep_windows"]).read_text())
        idx["pressure_bar"], idx["ensemble"] = 1.0, "NPT"  # pretend the parent was NPT
        Path(first["fep_windows"]).write_text(json.dumps(idx))
        second = run_fep(**common, lambda_indices="0", restart_windows_file=first["fep_windows"],
                         pressure_bar=0, output_dir=str(tmp_path / "run"))
        assert second["success"] is False and second["code"] == "fep_windows_incompatible"
