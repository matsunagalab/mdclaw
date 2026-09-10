"""The applied receipt: options as the tool used them, and the stage's facts.

The synthetic results below follow the shapes the real tools returned during
the 2026-09-10 campaign (prepare_complex on 013_membrane_6ps2, solvate on
049_nucleic_1iv6, embed_in_membrane on 014_membrane_6zdv, min/eq/prod on
011_membrane_6kuy, build_amber_system on 014).
"""

import json

import pytest

from mdclaw._receipt import build_receipt, option_lines


class TestOptionLines:
    def test_each_passed_option_reports_what_the_tool_used(self):
        result = {"parameters": {"water_model": "tip3p", "dist": 15.0, "saltcon": 0.15,
                                 "water_model_source": "inherited from solv node solv_001"},
                  "output_dir": "/j/nodes/solv_001/artifacts", "platform": "CUDA"}
        explicit = {"water_model": "TIP3P", "dist": 12.0, "platform": "cuda",
                    "restraint_atoms": "backbone", "output_dir": "/scratch/out", "pdb_file": "/x.pdb",
                    "job_dir": "/j", "node_id": "solv_001"}
        lines, ignored = option_lines(result, explicit, node_type="solv", node_mode=True)
        by_name = {line["name"]: line for line in lines}
        assert by_name["water_model"]["status"] == "applied"
        assert by_name["water_model"]["source"] == "inherited from solv node solv_001"
        assert by_name["dist"] == {"name": "dist", "requested": 12.0, "effective": 15.0, "status": "changed"}
        assert by_name["platform"]["status"] == "applied"
        assert by_name["restraint_atoms"]["status"] == "not_reported"
        assert "job_dir" not in by_name and "node_id" not in by_name
        assert [row["name"] for row in ignored] == ["output_dir", "pdb_file"]
        assert ignored[0]["status"] == "superseded_by_dag"
        assert ignored[0]["effective"] == "/j/nodes/solv_001/artifacts"

    def test_outside_node_mode_nothing_is_superseded(self):
        lines, ignored = option_lines({"output_dir": "/o"}, {"output_dir": "/o", "pdb_file": "/x"},
                                      node_type="solv", node_mode=False)
        assert ignored == []
        assert {line["name"]: line["status"] for line in lines} == {"output_dir": "applied",
                                                                     "pdb_file": "not_reported"}


PREP = {
    "success": True, "solvent_type": "explicit", "source_structure_id": "candidate_001",
    "proteins": [{"chain_id": "A", "success": True,
                  "statistics": {"final_residues": 283, "final_atoms": 4627},
                  "terminal_caps": {"n_terminal": None, "c_terminal": None},
                  "missing_residue_detection": {"reference_sequence_length": 506,
                                                "modeled_residues": 283, "status": "detected"}}],
    "preparation_summary": {"residue_range_groups": [
        {"chain_id": "A", "ranges": ["A:28-230", "A:263-342"], "residue_count": 283}]},
    "ligands": [{"ligand_id": "AMH", "ligand_instance_id": "A:AMH:90", "success": True,
                 "net_charge": 0, "protonation_method": "dimorphite", "protonation_ph": 7.0,
                 "sdf_file": "/x.sdf"}],
    "disulfide_bonds": [{"cys1": {"chain": "A", "resnum": 106}, "cys2": {"chain": "A", "resnum": 191}},
                        {"cys1": {"chain": "A", "resnum": 184}, "cys2": {"chain": "A", "resnum": 190}}],
    "merge_result": {"statistics": {"total_atoms": 4627, "total_residues": 283, "total_chains": 1}},
    "component_disposition_summary": {"excluded_component_count": 0},
    "residue_range_coverage": {"huge": "x" * 10},
}


class TestStageFacts:
    def test_prep_receipt_answers_what_agents_checked_by_hand(self):
        receipt = build_receipt(tool_name="prepare_complex", node_type="prep", result=PREP,
                                explicit={"residue_ranges": ["A:28-230", "A:263-342"]}, node_mode=True)
        facts = receipt["facts"]
        assert facts["chains"] == [{"chain": "A", "residues": 283, "atoms": 4627,
                                    "termini": "charged termini (no caps)"}]
        assert facts["pieces"] == [{"chain": "A", "ranges": ["A:28-230", "A:263-342"], "residues": 283}]
        assert facts["ligands"] == [{"id": "AMH", "instance": "A:AMH:90", "net_charge": 0,
                                     "protonation": "dimorphite", "ph": 7.0}]
        assert facts["disulfides"] == ["A106-A191", "A184-A190"]
        assert facts["missing_residues"] == [{"chain": "A", "unmodeled_residues": 223,
                                              "reference_length": 506, "modeled": 283}]
        assert "no bond is formed across a gap" in facts["gap_policy"]
        assert facts["atoms"] == 4627
        summary = receipt["summary"]
        assert summary.startswith("prepared: 1 protein chain(s) A (283 residues)")
        assert "chain A as 2 pieces (A:28-230, A:263-342; gaps left open)" in summary
        assert "223 residues unmodeled" in summary
        assert "1 ligand(s): AMH (+0)" in summary
        assert "2 disulfide(s)" in summary
        assert receipt["options"][0]["status"] == "not_reported"  # ranges are reported in facts, not parameters
        assert json.dumps(receipt).count("x" * 10) == 0  # large blocks never enter the receipt

    def test_solvate_receipt(self):
        result = {"parameters": {"water_model": "tip3p", "dist": 15.0, "salt": True, "salt_c": "Na+",
                                 "salt_a": "Cl-", "saltcon": 0.15},
                  "statistics": {"total_atoms": 44552}, "solute_net_charge_e": 3,
                  "ion_counts": {"cation_species": "Na+", "cation_count": 38,
                                 "anion_species": "Cl-", "anion_count": 14},
                  "box_dimensions": {"box_a": 75.38, "box_b": 75.38, "box_c": 75.38, "is_cubic": True}}
        receipt = build_receipt(tool_name="solvate_structure", node_type="solv", result=result,
                                explicit={"water_model": "tip3p", "saltcon": 0.15}, node_mode=True)
        assert receipt["facts"]["box_angstrom"] == [75.4, 75.4, 75.4]
        assert receipt["facts"]["ions"] == {"cation": "Na+", "cations": 38, "anion": "Cl-", "anions": 14}
        assert receipt["summary"] == ("solvated: 44,552 atoms, box 75.4×75.4×75.4 Å, water tip3p, "
                                      "Na+ 38 / Cl- 14 (0.15 M), solute charge +3 e")
        assert all(line["status"] == "applied" for line in receipt["options"])

    def test_membrane_receipt(self):
        result = {"parameters": {"lipids": "DPPC", "ratio": "1", "dist": 15.0, "salt": True,
                                 "salt_c": "Na+", "salt_a": "Cl-", "saltcon": 0.15, "water_model": "tip3p"},
                  "statistics": {"total_atoms": 137471, "protein_atoms": 4628,
                                 "neutralization": {"applied": True, "complete": True, "net_charge": 8,
                                                    "water_residues": 29573, "cations_requested": 80,
                                                    "anions_requested": 88}},
                  "orientation": {"method": "opm-homolog"},
                  "box_dimensions": {"box_a": 113.107, "box_b": 113.107, "box_c": 112.456, "is_cubic": False}}
        receipt = build_receipt(tool_name="embed_in_membrane", node_type="solv", result=result,
                                explicit={"lipids": ["DPPC"]}, node_mode=True)
        assert receipt["summary"].startswith("membrane: lipids DPPC, 137,471 atoms (protein 4,628), "
                                             "box 113.1×113.1×112.5 Å, water tip3p, Na+ 80 / Cl- 88 (0.15 M)")
        assert "solute charge +8 e" in receipt["summary"]
        assert "orientation opm-homolog" in receipt["summary"]
        assert receipt["facts"]["neutralized"] is True

    def test_topology_receipt_names_the_files_and_the_water_source(self):
        result = {"parameters": {"forcefield": "ff14SB", "water_model": "tip3p", "is_membrane": True,
                                 "water_model_source": "inherited from solv node solv_001",
                                 "forcefield_source": "paired with water model 'tip3p'"},
                  "statistics": {"num_atoms": 137471, "num_residues": 30895},
                  "system_net_charge_e": 2.4e-12,
                  "forcefield_provenance": {"openmm_xml": ["amber/protein.ff14SB.xml",
                                                           "amber/tip3p_standard.xml", "amber/lipid21.xml"],
                                            "small_molecule_forcefield": "gaff-2.11",
                                            "ligand_molecules": ["c1ccccc1"]},
                  "system_signature": {"hmr": True},
                  "topology_validation": {"status": "passed"}}
        receipt = build_receipt(tool_name="build_amber_system", node_type="topo", result=result,
                                explicit={"is_membrane": True, "pdb_file": "/manual.pdb"}, node_mode=True)
        assert receipt["summary"] == ("topology: protein.ff14SB + tip3p_standard + lipid21, water tip3p "
                                      "(inherited from solv node solv_001), HMR on, 1 ligand(s) via gaff-2.11, "
                                      "137,471 atoms / 30,895 residues, net charge +0.00 e, validation passed")
        assert receipt["ignored_options"][0]["name"] == "pdb_file"
        assert receipt["options"][0] == {"name": "is_membrane", "requested": True, "effective": True,
                                         "status": "applied"}

    def test_minimization_receipt(self):
        result = {"minimization": {"max_iterations": 50000, "restraint_force_constant": 100.0,
                                   "energy_initial_kj_mol": 894062.55, "energy_final_kj_mol": -1648623.47,
                                   "max_force_final_kj_mol_nm": 2695.52},
                  "restraint_atoms": "solute_heavy", "restraint_count": 2150,
                  "lipid_headgroup_restraint_count": 339, "platform": "CUDA"}
        receipt = build_receipt(tool_name="run_minimization", node_type="min", result=result,
                                explicit={"max_iterations": 50000}, node_mode=True)
        assert receipt["summary"] == ("minimized: up to 50,000 iterations, energy 894,062.55 → -1,648,623.47 kJ/mol, "
                                      "max force 2,695.52 kJ/mol/nm, restraints solute_heavy 2,150 atoms "
                                      "+ 339 lipid headgroups, platform CUDA")

    def test_equilibration_receipt_shows_a_nan_retry(self):
        result = {"stages_completed": ["NVT", "NPT"], "effective_nvt_time_ns": 1.0, "effective_npt_time_ns": 1.0,
                  "nvt_steps": 500000, "npt_steps": 500000, "timestep_fs": 2.0, "timestep_fs_requested": 4.0,
                  "restraint_atoms": "solute_heavy", "restraint_count": 2150, "platform": "CUDA",
                  "restart_from_node_id": "min_002", "restart_from_node_type": "min",
                  "nvt_heating": {"retried": True},
                  "integrator_signature": {"integrator": "LangevinMiddleIntegrator",
                                           "temperature_kelvin": 310.0, "timestep_fs": 2.0},
                  "system_signature": {"pressure_bar": 1.0, "hmr": True, "ensemble": "NPT"}}
        receipt = build_receipt(tool_name="run_equilibration", node_type="eq", result=result,
                                explicit={"timestep_fs": 4.0, "nvt_time_ns": 1.0}, node_mode=True)
        assert receipt["summary"] == ("equilibrated: NVT 1 ns + NPT 1 ns, at 310 K / 1 bar, "
                                      "2 fs (requested 4; NaN retry), HMR, restraints solute_heavy 2,150 atoms, "
                                      "from min_002, platform CUDA")
        by_name = {line["name"]: line for line in receipt["options"]}
        assert by_name["timestep_fs"]["status"] == "changed" and by_name["timestep_fs"]["effective"] == 2.0
        assert receipt["facts"]["heating_retried"] is True

    def test_production_receipt(self):
        result = {"ensemble": "NPT", "simulation_time_ns": 1.2, "temperature_kelvin": 310.0,
                  "pressure_bar": 1.0, "timestep_fs": 4.0, "hmr": True, "platform": "CUDA",
                  "restart_from_node_id": "eq_002",
                  "restart_integrator_changes": ["timestep_fs: restart=2.0, current=4.0"]}
        receipt = build_receipt(tool_name="run_production", node_type="prod", result=result,
                                explicit={"simulation_time_ns": 1.2}, node_mode=True)
        assert receipt["summary"] == ("production: 1.2 ns NPT, at 310 K / 1 bar, 4 fs, HMR, from eq_002, "
                                      "integrator settings changed from the restart (see warnings), platform CUDA")

    def test_unknown_stage_gets_the_generic_receipt(self):
        receipt = build_receipt(tool_name="analyze_rmsd", node_type="analyze",
                                result={"success": True, "frames": 100, "output_file": "/r.csv"},
                                explicit={}, node_mode=True)
        assert receipt["summary"] == "completed"
        assert receipt["facts"] == {"frames": 100, "output_file": "/r.csv"}


class TestCliReceipt:
    def test_a_stage_tool_result_starts_with_the_receipt(self, tmp_path, capsys):
        from mdclaw._cli import main
        from mdclaw._node import create_node, update_job_params

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        update_job_params(str(job_dir), {"solvent_regime": "explicit"})
        source = create_node(str(job_dir), "source")["node_id"]
        pdb = tmp_path / "x.pdb"
        pdb.write_text(
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            "ATOM      2  CA  ALA A   1      12.104  13.207  12.011  1.00 20.00           C\n"
            "END\n"
        )
        with pytest.raises(SystemExit) as exc_info:
            main(["--output", "full", "--job-dir", str(job_dir), "--node-id", source,
                  "register_local_structure", "--file-path", str(pdb), "--copy"])
        assert exc_info.value.code == 0, capsys.readouterr().out
        payload = json.loads(capsys.readouterr().out)
        keys = list(payload)
        assert keys.index("applied") == keys.index("message") + 1
        applied = payload["applied"]
        assert applied["tool"] == "register_local_structure"
        assert {line["name"] for line in applied["options"]} == {"file_path", "copy"}
        assert payload["message"].startswith(f"{source} completed: source")
        assert applied["summary"] == payload["message"].split(": ", 1)[1]
