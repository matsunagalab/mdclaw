# MDClaw Methods Draft: batch_2_4m3j_A / prod_001

## Methods Draft

The initial structure was obtained from RCSB PDB entry 4M3J. The selected chain(s), A, B, were prepared with MDClaw. Missing atoms and standard residue preparation were handled using PDB2PQR, PROPKA, and protonation states were assigned at pH 7.4. Special components were recorded as follows: no special components were recorded.

The prepared structure was solvated using an explicit opc water model with a cubic box and a 15 Å buffer. Ions were added according to 0.15 M salt concentration. Amber topology and coordinate files were generated with AmberTools/tleap using the ff19SB protein force field. Small-molecule ligand parameterization was not used.

Molecular dynamics simulations were performed with OpenMM. The system was equilibrated using energy minimization; NVT equilibration for 2500 steps; NPT equilibration for 5000 steps; 300 K; 1 bar; restraints on CA. Production simulations were run for 0.1 ns at 300 K using a 4 fs timestep, hydrogen mass repartitioning, and trajectory/energy outputs were recorded using the job's recorded reporter configuration. The workflow lineage used for this Methods draft was fetch_001 -> prep_001 -> solv_001 -> topo_001 -> eq_001 -> prod_001.

## LLM-Friendly Template

```text
Template paragraph 1: structure preparation
The initial structure was obtained from {source_description}. The selected chain(s), {chains}, were prepared with MDClaw. Missing atoms and standard residue preparation were handled using {preparation_tools}, and protonation states were assigned at pH {ph}. If present, ligands, glycans, nucleic acids, metal ions, or post-translational modifications were treated as follows: {special_components}.

Template paragraph 2: system construction
The prepared structure was solvated using an explicit {water_model} water model with {box_description}. Ions were added to {salt_description}. Amber topology and coordinate files were generated with AmberTools/tleap using the {forcefield} protein force field{additional_forcefields_sentence}. Small-molecule ligands, if present, were parameterized using {ligand_parameterization}.

Template paragraph 3: MD protocol
Molecular dynamics simulations were performed with OpenMM. The system was energy-minimized and equilibrated using {equilibration_protocol}. Production simulations were run for {simulation_time} at {temperature} K using a {timestep} timestep, {constraints_or_hmr}, and coordinates/energies were saved every {output_frequency}. The exact workflow lineage was {lineage}.

Citation insertion hint
Use only citations corresponding to filled placeholders. For example, include OpenMM and AmberTools for all OpenMM/Amber runs; include ff19SB and OPC only when `{forcefield}=ff19SB` and `{water_model}=OPC`; include MODELLER, Boltz-2, AlphaFold DB, GLYCAM, OL15/OL3, GAFF, modXNA, or PLIP only when those tools or models appear in the lineage.
```

## Workflow Schematic

```mermaid
flowchart LR
    N0["fetch_001: fetch"]
    N1["prep_001: prep"]
    N0 --> N1
    N2["solv_001: solv"]
    N1 --> N2
    N3["topo_001: topo"]
    N2 --> N3
    N4["eq_001: eq"]
    N3 --> N4
    N5["prod_001: prod"]
    N4 --> N5
```

## Lineage Summary

| Node | Type | Label | Parents | Conditions | Artifacts |
| --- | --- | --- | --- | --- | --- |
| `fetch_001` | fetch | - | - | - | structure_file |
| `prep_001` | prep | - | fetch_001 | - | disulfide_bonds, merged_pdb |
| `solv_001` | solv | - | prep_001 | - | box_dimensions, solvated_pdb |
| `topo_001` | topo | - | solv_001 | - | leap_log, leap_script, parm7, rst7 |
| `eq_001` | eq | 300K | topo_001 | temperature_kelvin=300, pressure_bar=1.0 | checkpoint, final_structure, state_file |
| `prod_001` | prod | 0.1ns | eq_001 | simulation_time_ns=0.1 | checkpoint, energy, final_structure, trajectory |

## Citation Keys

`Eastman2024OpenMM8`, `Case2023AmberTools`, `Burley2025RCSBPDB`, `Dolinsky2004PDB2PQR`, `Olsson2011PROPKA3`, `Tian2020ff19SB`, `Izadi2014OPC`

## BibTeX

```bibtex
@article{Eastman2024OpenMM8,
  author = {Eastman, Peter and Galvelis, Raimondas and Peláez, Raúl P. and Abreu, Carlos R. A. and Farr, Stephen E. and Gallicchio, Emilio and Gorenko, Anton and Henry, Michael M. and Hu, Frank and Huang, Jing and Krämer, Andreas and Michel, Julien and Mitchell, John A. and Pande, Vijay S. and Rodrigues, João P. G. L. M. and Rodriguez-Guerra, Jaime and Simmonett, Andrew C. and Singh, Shiv Upadhyay and Swails, Jason and Turner, Peter and Wang, Lee-Ping and Zhang, I-Feng W. and Chodera, John D. and De Fabritiis, Gianni and Markland, Thomas E.},
  title = {{OpenMM} 8: Molecular Dynamics Simulation with Machine Learning Potentials},
  journal = {The Journal of Physical Chemistry B},
  year = {2024},
  volume = {128},
  number = {1},
  pages = {109--116},
  doi = {10.1021/acs.jpcb.3c06662}
}

@article{Case2023AmberTools,
  author = {Case, David A. and Aktulga, H. Metin and Belfon, Kellon A. A. and Cerutti, David S. and Cisneros, G. Andrés and Cruzeiro, Vinícius W. D. and Forouzesh, Negin and Giese, Timothy J. and Götz, Andreas W. and Gohlke, Holger and Izadi, Saeed and Kasavajhala, Koushik and Kaymak, Mustafa C. and King, Edward and Kurtzman, Thomas and Lee, Tai-Sung and Li, Pengfei and Liu, Junmei and Luchko, Tyler and Luo, Ray and Manathunga, M. and Machado, Matheus R. and Nguyen, Hai M. and O'Hearn, Kevin A. and Onufriev, Alexey V. and Pan, Feng and Pantano, Sergio and Qi, Ruxi and Rahnamoun, Ali and Risheh, Akbar and Schott-Verdugo, Stephan and Shajan, Ali and Swails, Jason and Wang, Junmei and Wei, Hai and Wu, Xiaojian and Wu, Yongxiu and Zhang, Sheng and Zhao, Shijun and Zhu, Qing and Cheatham, Thomas E. and Roe, Daniel R. and Roitberg, Adrian and Simmerling, Carlos and York, Darrin M. and Nagan, Michael C. and Merz, Kenneth M.},
  title = {{AmberTools}},
  journal = {Journal of Chemical Information and Modeling},
  year = {2023},
  volume = {63},
  number = {20},
  pages = {6183--6191},
  doi = {10.1021/acs.jcim.3c01153}
}

@article{Burley2025RCSBPDB,
  author = {Burley, Stephen K. and others},
  title = {Updated Resources for Exploring Experimentally-Determined {PDB} Structures and Computed Structure Models at the {RCSB} Protein Data Bank},
  journal = {Nucleic Acids Research},
  year = {2025},
  volume = {53},
  number = {D1},
  pages = {D564--D574},
  doi = {10.1093/nar/gkae1091}
}

@article{Dolinsky2004PDB2PQR,
  author = {Dolinsky, Todd J. and Nielsen, Jens E. and McCammon, James A. and Baker, Nathan A.},
  title = {{PDB2PQR}: An Automated Pipeline for the Setup, Execution, and Analysis of Poisson-Boltzmann Electrostatics Calculations},
  journal = {Nucleic Acids Research},
  year = {2004},
  volume = {32},
  number = {suppl_2},
  pages = {W665--W667},
  doi = {10.1093/nar/gkh381}
}

@article{Olsson2011PROPKA3,
  author = {Olsson, Mats H. M. and Søndergaard, Chresten R. and Rostkowski, Michal and Jensen, Jan H.},
  title = {{PROPKA3}: Consistent Treatment of Internal and Surface Residues in Empirical pKa Predictions},
  journal = {Journal of Chemical Theory and Computation},
  year = {2011},
  volume = {7},
  number = {2},
  pages = {525--537},
  doi = {10.1021/ct100578z}
}

@article{Tian2020ff19SB,
  author = {Tian, Chuan and Kasavajhala, Koushik and Belfon, Kellon A. A. and Raguette, Lauren and Huang, He and Migues, Angela N. and Bickel, John and Wang, Yuzhang and Pincay, Jorge and Wu, Qin and Simmerling, Carlos},
  title = {{ff19SB}: Amino-Acid-Specific Protein Backbone Parameters Trained against Quantum Mechanics Energy Surfaces in Solution},
  journal = {Journal of Chemical Theory and Computation},
  year = {2020},
  volume = {16},
  number = {1},
  pages = {528--552},
  doi = {10.1021/acs.jctc.9b00591}
}

@article{Izadi2014OPC,
  author = {Izadi, Saeed and Anandakrishnan, Ramu and Onufriev, Alexey V.},
  title = {Building Water Models: A Different Approach},
  journal = {The Journal of Physical Chemistry Letters},
  year = {2014},
  volume = {5},
  number = {21},
  pages = {3863--3871},
  doi = {10.1021/jz501780a}
}
```

## Provenance

- Job directory: `/Users/yasu/gdrive/work/mdclaw/batch_2_4m3j_A`
- Terminal node: `prod_001`
- Candidate terminal nodes: `prod_001`
- Lineage: `fetch_001` -> `prep_001` -> `solv_001` -> `topo_001` -> `eq_001` -> `prod_001`
- Lineage event count: 24

