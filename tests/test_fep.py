"""Unit tests for the ``fep`` server: protocol, mapping, tripeptide, MBAR, ddG node.

The pure-Python parts (protocol schedule, atom mapping on synthetic records,
the fragment cut, MBAR on a harmonic-oscillator toy, the ddG comparison node
over two analyze_fep nodes) run everywhere. ``TestHybridVacuum`` /
``TestVacuumPipeline`` build a real ACE-X-NME hybrid System with amber14 on the
Reference platform and ``TestUnfoldedLegPrep`` caps a real fragment with
clean_protein; those are marked ``slow``.
"""

from __future__ import annotations

import json
import math
import shutil
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
from mdclaw.fep.tripeptide import cut_fragment, extract_tripeptide
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
    """The cut itself (pure text, fast). Capping and the prep node run under
    ``TestUnfoldedLegPrep`` (slow: clean_protein needs PDBFixer / pdb2pqr)."""

    @staticmethod
    def _spec(pdb: Path, mutation: str):
        from mdclaw.fep.mutant import parse_single_mutation

        return parse_single_mutation(mutation, pdb)

    def test_cut_flanked_fragment(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        atoms, keep, warnings = cut_fragment(pdb.read_text().splitlines(), self._spec(pdb, "A:L12A"), 1)
        assert [k[1] for k in keep] == [11, 12, 13]
        assert {ln[22:26].strip() for ln in atoms} == {"11", "12", "13"}
        assert [int(ln[6:11]) for ln in atoms] == list(range(1, len(atoms) + 1))
        assert all(ln[21] == "A" for ln in atoms)  # chain id kept for the shared --mutation
        assert not warnings

    def test_truncated_at_chain_end(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        _atoms, keep, warnings = cut_fragment(pdb.read_text().splitlines(), self._spec(pdb, "A:G10A"), 2)
        assert [k[1] for k in keep] == [10, 11, 12]
        assert any("could be kept" in w for w in warnings)

    def test_chain_break_is_flagged(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb", gap_after=1)
        _atoms, _keep, warnings = cut_fragment(pdb.read_text().splitlines(), self._spec(pdb, "A:L12A"), 1)
        assert any("chain break" in w for w in warnings)

    def test_bad_mutation(self, tmp_path):
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        res = extract_tripeptide(mutation="A:L99A", pdb_file=str(pdb), output_dir=str(tmp_path / "out"))
        assert res["success"] is False
        assert res["code"] == "fep_mutation_residue_not_found"

    def test_missing_file_and_bad_flank(self, tmp_path):
        assert extract_tripeptide(mutation="A:L12A", pdb_file=str(tmp_path / "nope.pdb"))["code"] == "file_not_found"
        pdb = _write_pentapeptide(tmp_path / "pent.pdb")
        assert extract_tripeptide(mutation="A:L12A", pdb_file=str(pdb), flank=-1)["code"] == "invalid_parameter_value"

    def test_prep_parent_required_in_node_mode(self, tmp_path):
        """Under a source node there is no prepared structure to cut from; the
        node stays pending with a code that names the fix."""
        from mdclaw._node import create_node, init_progress_v3, read_node
        from tests.pipeline_helpers import complete_node_with_placeholders as complete

        jd = tmp_path / "job"
        jd.mkdir()
        init_progress_v3(str(jd))
        src = create_node(str(jd), "source")["node_id"]
        complete(str(jd), src, {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
        res = extract_tripeptide(mutation="A:L12A", job_dir=str(jd), node_id=prep)
        assert res["success"] is False and res["code"] == "fep_fragment_prep_required"
        assert read_node(str(jd), prep)["status"] == "pending"


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

    def test_parent_and_child_indexes_are_not_double_counted(self, tmp_path):
        """A fep -> fep child's index already chains the parent's segments;
        parenting the analyze node to both must not count them twice."""
        pytest.importorskip("pymbar")
        files, _, _ = _harmonic_windows(tmp_path)
        parent = json.loads(files[0].read_text())
        child_dir = tmp_path / "fep_002"
        child_dir.mkdir()
        windows = {}
        for k, rec in parent["windows"].items():
            wdir = child_dir / f"window_{int(k):02d}"
            wdir.mkdir()
            shutil.copy(rec["segments"][0]["energies_file"], wdir / "energies.npz")
            windows[k] = {"index": int(k), "segments": rec["segments"] + [
                {"node_id": "fep_002", "energies_file": str(wdir / "energies.npz")}]}
        child_file = child_dir / "fep_windows.json"
        child_file.write_text(json.dumps({**parent, "node_id": "fep_002", "windows": windows}))
        leaf_only = collect_windows([str(child_file)])
        both = collect_windows([str(files[0]), str(child_file)])
        assert not leaf_only["warnings"]
        assert all(len(both["windows"][k]) == len(leaf_only["windows"][k]) == 2 for k in range(5))
        assert both["warnings"] and "counted once" in both["warnings"][0]
        res = analyze_fep(fep_windows_files=[str(files[0]), str(child_file)], output_dir=str(tmp_path / "out"),
                          discard_fraction=0.0, subsample=False)
        assert res["success"], res
        assert res["n_samples_per_state"] == [800] * 5
        assert any("counted once" in w for w in res["warnings"])
        report = json.loads(Path(res["fep_result"]).read_text())
        assert [seg["n_after_discard"] for seg in report["per_window"][0]["segments"]] == [400, 400]

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

    def test_estimate_ddg_direct(self, tmp_path):
        folded = tmp_path / "folded.json"
        unfolded = tmp_path / "unfolded.json"
        folded.write_text(json.dumps({"mutation": {"label": "A:L99A"}, "dG_kj_mol": 10.0, "dG_error_kj_mol": 0.3,
                                      "warnings": ["low overlap 3-4"]}))
        unfolded.write_text(json.dumps({"mutation": {"label": "A:L99A"}, "dG_kj_mol": 4.0, "dG_error_kj_mol": 0.4}))
        res = estimate_ddg(str(folded), str(unfolded), output_file=str(tmp_path / "out" / "ddg.json"))
        assert res["success"], res
        assert math.isclose(res["ddG_kj_mol"], 6.0)
        assert math.isclose(res["ddG_error_kj_mol"], 0.5)
        assert math.isclose(res["ddG_kcal_mol"], 6.0 / 4.184)
        assert any("[folded] low overlap" in w for w in res["warnings"])
        assert any("could not verify" in w for w in res["warnings"])  # no protocol / manifest to compare
        assert Path(res["ddg_file"]) == tmp_path / "out" / "ddg.json"
        # never inside the (immutable) analyze node's artifacts: default goes to the study's evidence/
        study = tmp_path / "study"
        (study / "jobs").mkdir(parents=True)
        res2 = estimate_ddg(str(folded), str(unfolded), study_dir=str(study))
        assert Path(res2["ddg_file"]).parent == study / "evidence"
        assert not (tmp_path / "ddg.json").exists()

    def test_estimate_ddg_refuses_different_legs(self, tmp_path):
        """Two legs of different mutations (or protocols, ensembles) are not one
        thermodynamic cycle."""
        folded = tmp_path / "folded.json"
        unfolded = tmp_path / "unfolded.json"
        folded.write_text(json.dumps({"mutation": {"label": "A:L99A"}, "dG_kj_mol": 10.0, "temperature_kelvin": 300.0}))
        unfolded.write_text(json.dumps({"mutation": {"label": "A:L2A"}, "dG_kj_mol": 4.0, "temperature_kelvin": 300.0}))
        res = estimate_ddg(str(folded), str(unfolded), output_file=str(tmp_path / "ddg.json"))
        assert res["success"] is False and res["code"] == "fep_legs_incompatible"
        assert "mutation" in res["errors"][0]
        unfolded.write_text(json.dumps({"mutation": {"label": "A:L99A"}, "dG_kj_mol": 4.0, "temperature_kelvin": 310.0}))
        res = estimate_ddg(str(folded), str(unfolded), output_file=str(tmp_path / "ddg.json"))
        assert res["code"] == "fep_legs_incompatible" and "temperature_kelvin" in res["errors"][0]
        assert not (tmp_path / "ddg.json").exists()

    def test_estimate_ddg_rejects_non_result(self, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text("{}")
        res = estimate_ddg(str(bad), str(bad))
        assert res["success"] is False and res["code"] == "fep_result_invalid"
        assert estimate_ddg()["code"] == "fep_result_invalid"


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
        # the CLI's nargs='+' hands "a,b" over as one token; it is split, not rejected
        fep3 = create_node(str(jd), "fep", parent_node_ids=[eq])["node_id"]
        complete(str(jd), fep3, {"fep_windows": "artifacts/fep_windows.json"})
        joined = create_node(str(jd), "analyze", parent_node_ids=[f"{fep1['node_id']},{fep3}"],
                             conditions={"analysis_data_scope": "alchemical"})
        assert joined["success"], joined
        from mdclaw._node import read_node
        assert read_node(str(jd), joined["node_id"])["parent_node_ids"] == [fep1["node_id"], fep3]
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
# ddG node: comparison analyze over the two legs' analyze_fep nodes            #
# --------------------------------------------------------------------------- #

def _fep_leg_result(tmp_path: Path, name: str, n_states: int = 5) -> Path:
    """A real analyze_fep result (harmonic toy) to hang on a leg's analyze node."""
    (tmp_path / name).mkdir()
    files, _protocol, _ = _harmonic_windows(tmp_path / name, n_states=n_states)
    res = analyze_fep(fep_windows_files=[str(f) for f in files], output_dir=str(tmp_path / f"{name}_an"),
                      discard_fraction=0.0, subsample=False)
    assert res["success"], res
    return Path(res["fep_result"])


def _two_leg_job(tmp_path: Path, *, mark_unfolded: bool = True, with_unfolded: bool = True,
                 unfolded_n_states: int = 5) -> tuple[Path, str, dict[str, str]]:
    """source -> prep_001 (protein) -> folded chain; prep_002 (fragment, child of
    prep_001) -> unfolded chain; both chains end in a completed analyze_fep node."""
    from mdclaw._node import create_node, init_progress_v3
    from tests.pipeline_helpers import complete_node_with_placeholders as complete

    jd = tmp_path / "job"
    jd.mkdir()
    init_progress_v3(str(jd))
    src = create_node(str(jd), "source")["node_id"]
    complete(str(jd), src, {"source_bundle": "artifacts/sb.json"})
    prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
    complete(str(jd), prep, {"merged_pdb": "artifacts/merge/merged.pdb"})
    starts = [("folded", prep, 5)]
    if with_unfolded:
        frag = create_node(str(jd), "prep", parent_node_ids=[prep])["node_id"]
        complete(str(jd), frag, {"merged_pdb": "artifacts/merge/merged.pdb"},
                 metadata={"tool": "extract_tripeptide", "leg_role": "unfolded"} if mark_unfolded
                 else {"tool": "prepare_complex"})
        starts.append(("unfolded", frag, unfolded_n_states))
    triple = {"system_xml": "artifacts/system.system.xml", "topology_pdb": "artifacts/system.topology.pdb",
              "state_xml": "artifacts/system.state.xml"}
    legs: dict[str, str] = {}
    for role, top_prep, n_states in starts:
        solv = create_node(str(jd), "solv", parent_node_ids=[top_prep])["node_id"]
        complete(str(jd), solv, {"solvated_pdb": "artifacts/s.pdb"})
        topo = create_node(str(jd), "topo", parent_node_ids=[solv])["node_id"]
        complete(str(jd), topo, {**triple, "hybrid_manifest": "artifacts/hybrid_manifest.json",
                                 "fep_protocol": "artifacts/fep_protocol.json"})
        mn = create_node(str(jd), "min", parent_node_ids=[topo])["node_id"]
        complete(str(jd), mn, {"state": "artifacts/min.xml"})
        eq = create_node(str(jd), "eq", parent_node_ids=[mn])["node_id"]
        complete(str(jd), eq, {"state": "artifacts/eq.xml"})
        fep = create_node(str(jd), "fep", parent_node_ids=[eq])["node_id"]
        complete(str(jd), fep, {"fep_windows": "artifacts/fep_windows.json"})
        an = create_node(str(jd), "analyze", parent_node_ids=[fep],
                         conditions={"analysis_data_scope": "alchemical"})["node_id"]
        dst = jd / "nodes" / an / "artifacts" / "fep_result.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_fep_leg_result(tmp_path, role, n_states), dst)
        complete(str(jd), an, {"fep_result": "artifacts/fep_result.json"},
                 metadata={"analysis": "fep_mbar", "mutation": "A:L99A"})
        legs[role] = an
    return jd, prep, legs


class TestDdgNode:
    def test_ddg_node_over_two_legs(self, tmp_path):
        pytest.importorskip("pymbar")
        from mdclaw._cli import _discover_tools
        from mdclaw._envelope import next_step
        from mdclaw._node import create_node, read_node

        jd, prep, legs = _two_leg_job(tmp_path)
        tools = _discover_tools()
        # envelope: both legs analysed -> create the comparison node over them
        step = next_step(str(jd), legs["folded"], tools)
        assert step["action"] == "create" and step["node_type"] == "analyze", step
        assert f"--parent-node-ids {legs['folded']} {legs['unfolded']}" in step["create_command"]
        assert '"analysis_data_scope": "comparison"' in step["create_command"]
        assert step["stage_tools"] == ["estimate_ddg"]
        # the unfolded leg gets the same suggestion (same parent order)
        assert next_step(str(jd), legs["unfolded"], tools)["create_command"] == step["create_command"]

        # parents in the "wrong" order: roles come from the DAG, not the order
        ddg = create_node(str(jd), "analyze", parent_node_ids=[legs["unfolded"], legs["folded"]],
                          conditions={"analysis_data_scope": "comparison"})
        assert ddg["success"], ddg
        ddg_id = ddg["node_id"]
        pending = next_step(str(jd), legs["folded"], tools)
        assert pending["action"] == "run" and pending["node_id"] == ddg_id
        assert pending["stage_tools"][0] == "estimate_ddg"

        res = estimate_ddg(job_dir=str(jd), node_id=ddg_id)
        assert res["success"], res
        assert res["legs"]["folded"]["node_id"] == legs["folded"]
        assert res["legs"]["unfolded"]["node_id"] == legs["unfolded"]
        assert math.isclose(res["ddG_kj_mol"], res["legs"]["folded"]["dG_kj_mol"] - res["legs"]["unfolded"]["dG_kj_mol"])
        node = read_node(str(jd), ddg_id)
        assert node["status"] == "completed"
        assert node["metadata"]["analysis"] == "fep_ddg"
        assert node["metadata"]["legs"]["unfolded"]["node_id"] == legs["unfolded"]
        report = json.loads((jd / "nodes" / ddg_id / node["artifacts"]["ddg"]).read_text())
        assert report["analysis"] == "fep_ddg" and report["node_id"] == ddg_id
        assert Path(res["ddg_file"]).resolve() == (jd / "nodes" / ddg_id / "artifacts" / "ddg.json").resolve()
        done = next_step(str(jd), legs["folded"], tools)
        assert done["action"] == "done" and done["node_id"] == ddg_id
        assert next_step(str(jd), ddg_id, tools)["action"] == "done"

    def test_only_folded_leg_points_at_the_fragment_prep(self, tmp_path):
        pytest.importorskip("pymbar")
        from mdclaw._cli import _discover_tools
        from mdclaw._envelope import next_step

        jd, prep, legs = _two_leg_job(tmp_path, with_unfolded=False)
        step = next_step(str(jd), legs["folded"], _discover_tools())
        assert step["action"] == "create" and step["node_type"] == "prep", step
        assert f"--parent-node-ids {prep}" in step["create_command"]
        assert step["stage_tools"][0] == "extract_tripeptide"
        assert "extract_tripeptide --mutation A:L99A" in step["run_command"]

    def test_ddg_node_refuses_incompatible_legs(self, tmp_path):
        pytest.importorskip("pymbar")
        from mdclaw._node import create_node, read_node

        jd, _prep, legs = _two_leg_job(tmp_path, unfolded_n_states=7)
        ddg = create_node(str(jd), "analyze", parent_node_ids=[legs["folded"], legs["unfolded"]],
                          conditions={"analysis_data_scope": "comparison"})["node_id"]
        res = estimate_ddg(job_dir=str(jd), node_id=ddg)
        assert res["success"] is False and res["code"] == "fep_legs_incompatible"
        assert "n_states" in res["errors"][0]
        assert read_node(str(jd), ddg)["status"] == "pending"  # nothing ran; fix the leg and rerun

    def test_ddg_node_leg_roles(self, tmp_path):
        pytest.importorskip("pymbar")
        from mdclaw._node import create_node, read_node

        jd, _prep, legs = _two_leg_job(tmp_path, mark_unfolded=False)
        ddg = create_node(str(jd), "analyze", parent_node_ids=[legs["folded"], legs["unfolded"]],
                          conditions={"analysis_data_scope": "comparison"})["node_id"]
        res = estimate_ddg(job_dir=str(jd), node_id=ddg)
        assert res["success"] is False and res["code"] == "fep_leg_role_ambiguous"
        assert read_node(str(jd), ddg)["status"] == "pending"
        # declared subjects in parent order resolve it
        ddg2 = create_node(str(jd), "analyze", parent_node_ids=[legs["unfolded"], legs["folded"]],
                           conditions={"analysis_data_scope": "comparison",
                                       "analysis_subjects": [{"label": "unfolded"}, {"label": "folded"}]})["node_id"]
        res = estimate_ddg(job_dir=str(jd), node_id=ddg2)
        assert res["success"], res
        assert res["legs"]["folded"]["node_id"] == legs["folded"]

    def test_ddg_node_scope_and_parent_checks(self, tmp_path):
        pytest.importorskip("pymbar")
        from mdclaw._node import create_node, read_node
        from tests.pipeline_helpers import complete_node_with_placeholders as complete

        jd, _prep, legs = _two_leg_job(tmp_path)
        wrong_scope = create_node(str(jd), "analyze", parent_node_ids=[legs["folded"], legs["unfolded"]],
                                  conditions={"analysis_data_scope": "production_chain"})["node_id"]
        res = estimate_ddg(job_dir=str(jd), node_id=wrong_scope)
        assert res["code"] == "fep_ddg_scope_invalid" and read_node(str(jd), wrong_scope)["status"] == "pending"
        # a parent that is not an analyze_fep result
        other = create_node(str(jd), "analyze", parent_node_ids=[legs["folded"]],
                            conditions={"analysis_data_scope": "production_chain"})["node_id"]
        complete(str(jd), other, {"summary": "artifacts/summary.json"}, metadata={"analysis": "something_else"})
        bad = create_node(str(jd), "analyze", parent_node_ids=[other, legs["unfolded"]],
                          conditions={"analysis_data_scope": "comparison"})["node_id"]
        res = estimate_ddg(job_dir=str(jd), node_id=bad)
        assert res["code"] == "fep_ddg_parents_invalid" and other in res["errors"][0]


# --------------------------------------------------------------------------- #
# unfolded-leg prep node with a real cap build (slow)                           #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
class TestUnfoldedLegPrep:
    @staticmethod
    def _prepared_peptide(tmp_path: Path) -> Path:
        """Residues 97-107 of the 6KUY fixture (real crystal geometry), completed
        and protonated by PDBFixer: stands in for prepare_complex's merged.pdb."""
        pdbfixer = pytest.importorskip("pdbfixer")
        from openmm import app

        fixture = Path(__file__).parent / "data" / "6kuy_trp99_piece1.pdb"
        keep = [ln for ln in fixture.read_text().splitlines()
                if ln.startswith("ATOM") and 97 <= int(ln[22:26]) <= 107]
        segment = tmp_path / "segment.pdb"
        segment.write_text("\n".join([*keep, "TER", "END", ""]))
        fixer = pdbfixer.PDBFixer(filename=str(segment))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()
        fixer.addMissingAtoms()
        fixer.addMissingHydrogens(7.0)
        out = tmp_path / "prepared.pdb"
        with out.open("w") as fh:
            app.PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)
        return out

    def test_direct_mode_caps_and_keeps_numbering(self, tmp_path):
        prepared = self._prepared_peptide(tmp_path)
        res = extract_tripeptide(mutation="A:W99A", pdb_file=str(prepared), output_dir=str(tmp_path / "out"))
        assert res["success"], res
        lines = [ln for ln in Path(res["merged_pdb"]).read_text().splitlines() if ln.startswith(("ATOM", "HETATM"))]
        residues = {(ln[17:20].strip(), int(ln[22:26])) for ln in lines}
        assert {"ACE", "NME"} <= {r for r, _ in residues}
        assert {n for r, n in residues if r in ("TYR", "TRP")} == {98, 99, 100}  # numbering shared with the folded leg
        assert all(ln[21] == "A" for ln in lines)                                 # chain id too
        assert res["caps"] == {"n_terminal": "ACE", "c_terminal": "NME"}
        assert res["leg_role"] == "unfolded" and res["residues"] == ["A:TYR98", "A:TRP99", "A:TYR100"]
        cmap = json.loads(Path(res["chain_identity_map"]).read_text())
        assert cmap["components"][0]["pdb_chain_id"] == "A" and cmap["components"][0]["atom_count"] == len(lines)

    def test_prep_node_under_the_protein_prep(self, tmp_path):
        from mdclaw._node import create_node, init_progress_v3, read_node, resolve_node_inputs
        from mdclaw.fep.analysis import leg_role_of
        from tests.pipeline_helpers import complete_node_with_placeholders as complete

        prepared = self._prepared_peptide(tmp_path)
        jd = tmp_path / "job"
        jd.mkdir()
        init_progress_v3(str(jd))
        src = create_node(str(jd), "source")["node_id"]
        complete(str(jd), src, {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
        merged = jd / "nodes" / prep / "artifacts" / "merge" / "merged.pdb"
        merged.parent.mkdir(parents=True)
        shutil.copy(prepared, merged)
        complete(str(jd), prep, {"merged_pdb": "artifacts/merge/merged.pdb"})

        frag = create_node(str(jd), "prep", parent_node_ids=[prep])["node_id"]
        res = extract_tripeptide(mutation="A:W99A", job_dir=str(jd), node_id=frag)
        assert res["success"], res
        node = read_node(str(jd), frag)
        assert node["status"] == "completed"
        assert node["metadata"]["leg_role"] == "unfolded"
        assert node["metadata"]["derived_from_prep_node_id"] == prep
        assert node["metadata"]["unfolded_model"]["residues"] == ["A:TYR98", "A:TRP99", "A:TYR100"]
        assert set(node["artifacts"]) >= {"merged_pdb", "fragment_pdb", "chain_identity_map", "disulfide_bonds"}
        # the solv stage under the fragment resolves the fragment, not the protein
        solv = create_node(str(jd), "solv", parent_node_ids=[frag])["node_id"]
        inputs = resolve_node_inputs(str(jd), solv, "solv")
        assert inputs["pdb_file"] == str((jd / "nodes" / frag / "artifacts" / "merge" / "merged.pdb").resolve())
        assert inputs["disulfide_bonds"] == []
        assert leg_role_of(str(jd), solv) == "unfolded"
        assert leg_role_of(str(jd), prep) is None
        # a completed prep node runs once
        again = extract_tripeptide(mutation="A:W99A", job_dir=str(jd), node_id=frag)
        assert again["success"] is False


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
        # ... and so is every path in the per-window record (relative to its own directory)
        wj = json.loads((index_file.parent / "window_00" / "window.json").read_text())
        assert wj["energies_file"] == "energies.npz" and wj["state_file"] == "state.xml"
        assert wj["restarted_from"] is None
        assert rec["start_minimisation"]["after_kj_mol"] <= rec["start_minimisation"]["before_kj_mol"] + 1e-6

        assert raw["extended_from"] is None
        # fresh windows with (almost) no re-equilibration after the start minimisation are flagged
        assert any("re-heat" in w for w in first["warnings"]), first["warnings"]

        # analysis refuses an incomplete protocol and names the missing windows
        partial = analyze_fep(fep_windows_files=[str(index_file)], output_dir=str(tmp_path / "an0"),
                              discard_fraction=0.0, subsample=False)
        assert partial["code"] == "fep_windows_incomplete" and "[2]" in partial["errors"][0]

        # recovery of a "killed" node: sample only the missing window; the
        # finished ones are carried into the new index unchanged
        recover = run_fep(**common, lambda_indices="2", restart_windows_file=str(index_file),
                          output_dir=str(tmp_path / "run"))
        assert recover["success"], recover
        assert recover["lambda_indices"] == [2] and recover["carried_over_windows"] == [0, 1]
        idx_r = load_windows_index(recover["fep_windows"])
        assert sorted(idx_r["windows"]) == [0, 1, 2]
        assert all(len(idx_r["windows"][k]["segments"]) == 1 for k in (0, 1, 2))
        assert idx_r["windows"][0]["segments"][0]["energies_file"] == str(
            (index_file.parent / "window_00" / "energies.npz").resolve())
        raw_r = json.loads(Path(recover["fep_windows"]).read_text())
        assert not Path(raw_r["extended_from"]).is_absolute()
        assert all(not Path(v).is_absolute() for rec in raw_r["windows"].values()
                   for v in (rec.get("restarted_from"),) if v)
        rec_res = analyze_fep(fep_windows_files=[recover["fep_windows"]], output_dir=str(tmp_path / "an_r"),
                              discard_fraction=0.0, subsample=False)
        assert rec_res["success"], rec_res
        assert rec_res["n_samples_per_state"] == [20, 20, 20]

        # a failure on the very first window still leaves the carried-over
        # windows in a partial index on disk
        import mdclaw.fep.run as run_mod

        def _boom(**kwargs):
            raise RuntimeError("simulated crash before the first sample")

        monkeypatch.setattr(run_mod, "sample_window", _boom)
        crashed = run_fep(**common, lambda_indices="2", restart_windows_file=str(index_file),
                          output_dir=str(tmp_path / "run"))
        monkeypatch.undo()
        assert crashed["success"] is False and crashed["code"] == "fep_sampling_failed"
        assert crashed["indexed_windows"] == [0, 1] and crashed["sampled_windows"] == []
        partial_idx = json.loads(Path(crashed["fep_windows"]).read_text())
        assert partial_idx["complete"] is False and sorted(partial_idx["windows"]) == ["0", "1"]
        assert partial_idx["carried_over_windows"] == [0, 1]

        # a carried-over window needs no state.xml (only continued ones do)
        state1 = index_file.parent / "window_01" / "state.xml"
        hidden = state1.with_suffix(".hidden")
        state1.rename(hidden)
        no_state = run_fep(**common, lambda_indices="1", restart_windows_file=str(index_file),
                           output_dir=str(tmp_path / "run"))
        assert no_state["success"] is False and no_state["code"] == "fep_windows_missing"
        assert "carry it over" in no_state["errors"][0]
        carried = run_fep(**common, lambda_indices="2", restart_windows_file=str(index_file),
                          output_dir=str(tmp_path / "run"))
        assert carried["success"] and carried["carried_over_windows"] == [0, 1]
        hidden.rename(state1)

        # second node: window 2, plus continue windows 0-1 from the first index
        second = run_fep(**common, lambda_indices="all", restart_windows_file=str(index_file),
                         output_dir=str(tmp_path / "run"))
        assert second["success"], second
        idx2 = load_windows_index(second["fep_windows"])
        assert sorted(idx2["windows"]) == [0, 1, 2]
        assert len(idx2["windows"][0]["segments"]) == 2 and len(idx2["windows"][2]["segments"]) == 1
        assert idx2["windows"][0]["start_minimisation"] is None  # continued, not restarted from eq
        assert Path(idx2["windows"][0]["segments"][0]["energies_file"]).is_file()
        # a continued window's own record points back at the parent state it
        # started from, as a path relative to that window's directory
        w0_dir = Path(second["fep_windows"]).parent / "window_00"
        wj0 = json.loads((w0_dir / "window.json").read_text())
        assert wj0["restarted_from"] and not Path(wj0["restarted_from"]).is_absolute()
        assert (w0_dir / wj0["restarted_from"]).resolve() == (index_file.parent / "window_00" / "state.xml").resolve()
        assert idx2["windows"][0]["restarted_from"] == str((index_file.parent / "window_00" / "state.xml").resolve())

        # parenting the analysis to parent + child must not double count the parent's samples
        both = analyze_fep(fep_windows_files=[str(index_file), second["fep_windows"]],
                           output_dir=str(tmp_path / "an_both"), discard_fraction=0.0, subsample=False)
        assert both["success"], both
        assert both["n_samples_per_state"] == [40, 40, 20]
        assert any("counted once" in w for w in both["warnings"])

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
        idx["pressure_bar"], idx["ensemble"] = None, "NVT"
        Path(first["fep_windows"]).write_text(json.dumps(idx))
        hot = run_fep(**common, lambda_indices="0", restart_windows_file=first["fep_windows"],
                      temperature_kelvin=310.0, output_dir=str(tmp_path / "run"))
        assert hot["success"] is False and hot["code"] == "fep_windows_incompatible"
        assert "310.0 K" in hot["errors"][0]

    def test_hybrid_topology_pdb_keeps_amber_variant_names(self, tmp_path):
        """``_assemble_hybrid`` / ``_write_artifacts`` on vacuum end states whose
        ``topology.pdb`` spells the Amber protonation-state name (HIE): the
        loader normalises it to HIS, the hybrid must write HIE again."""
        import io

        openmm = pytest.importorskip("openmm")
        from openmm import app, unit

        from mdclaw.fep.build import _Inputs, _assemble_hybrid, _write_artifacts
        from mdclaw.fep.mutant import MutationSpec

        ff = app.ForceField("amber14-all.xml")
        endstates: dict = {}
        for label, resname in (("wt", "HIS"), ("mut", "ALA")):
            top, pos = TestHybridVacuum._peptide(resname, tmp_path)
            system = ff.createSystem(top, nonbondedMethod=app.NoCutoff, constraints=app.HBonds)
            sim = app.Simulation(top, system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
            sim.context.setPositions(pos)
            sim.minimizeEnergy(maxIterations=200)
            state = sim.context.getState(getPositions=True, getVelocities=True, getEnergy=True)
            d = tmp_path / "endstates" / label
            d.mkdir(parents=True)
            buf = io.StringIO()
            app.PDBFile.writeFile(top, state.getPositions(), buf, keepIds=True)
            # the topo contract writes the Amber name; PDBFile will read it back as HIS
            text = buf.getvalue().replace(" HIS A", " HIE A")
            (d / "topology.pdb").write_text(text)
            (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
            (d / "state.xml").write_text(openmm.XmlSerializer.serialize(state))
            endstates[label] = {"system_xml": str(d / "system.xml"), "topology_pdb": str(d / "topology.pdb"),
                                "state_xml": str(d / "state.xml"), "system_net_charge_e": 0.0,
                                "parameters": {}, "forcefield_provenance": None, "solvent_type": "vacuum"}
        assert " HIE A" in Path(endstates["wt"]["topology_pdb"]).read_text()
        spec = MutationSpec(chain_id="A", resseq=2, icode="", wt_resname="HIE", mut_resname="ALA", label="A:H2A")
        asm = _assemble_hybrid(endstates, spec, softcore_alpha=0.5, endpoint_tolerance_kj_mol=1.0)
        assert asm.validation["passed"], asm.validation
        assert [r.name for r in asm.top_a.residues()] == ["ACE", "HIE", "NME"]
        assert asm.build.mapping.residue_old_name == "HIE" and asm.build.mapping.residue_new_name == "ALA"

        out = tmp_path / "out"
        out.mkdir()
        inputs = _Inputs(pdb_file=tmp_path / "x.pdb", forcefield="amber14", water_model=None, box_dimensions=None,
                         is_membrane=False, ligand_chemistry=None, disulfide_bonds=None)
        written = _write_artifacts(asm, endstates, spec, {"backend": "test", "mutant_pdb": "x"},
                                   windows_from_schedule("0,0.5,1"), inputs, out, output_name="system",
                                   softcore_alpha=0.5, hmr=False, warnings=[])
        # (PDBFile writes non-standard names such as HIE as HETATM records, like every topo contract file)
        records = [ln for ln in written["files"]["topology_pdb"].read_text().splitlines()
                   if ln.startswith(("ATOM  ", "HETATM"))]
        names = {ln[17:20] for ln in records}
        assert "HIE" in names and "HIS" not in names
        # the appended ALA atoms sit inside the HIE residue, so it holds every atom that is not ACE / NME
        hie_atoms = [ln for ln in records if ln[17:20] == "HIE"]
        assert len(hie_atoms) == asm.build.mapping.n_hybrid - sum(1 for r in asm.top_a.residues() if r.name != "HIE"
                                                                   for _ in r.atoms())
        assert written["manifest"]["mapping"]["residue"]["old_name"] == "HIE"
        # the loader still reads the file (names are legal), and the count matches the System
        reloaded = app.PDBFile(str(written["files"]["topology_pdb"]))
        assert reloaded.topology.getNumAtoms() == asm.build.system.getNumParticles()
        del unit
