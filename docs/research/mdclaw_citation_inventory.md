# MDClaw Citation Inventory

Last researched: 2026-05-05

This file is a master citation inventory for MDClaw-generated Methods text. It is intentionally broader than any single run: when drafting Methods from a selected `job_dir` and terminal `node_id`, include only the entries corresponding to tools, force fields, models, and databases that appear in that node lineage.

Primary repository sources checked:

- `mdclaw/*.py`
- `skills/*/SKILL.md`
- `README.md`
- `CLAUDE.md`
- `docs/developer/*.md`
- `environment.yml`
- `pyproject.toml`
- `container/Dockerfile`

## Use Policy

- Always cite the MD engine and topology/parameterization toolchain actually used by the lineage.
- Cite force fields and water models explicitly when they appear in `topo` metadata, `progress.json` params, or relevant `node.json` artifacts.
- Cite data resources only when the lineage used them: for example, RCSB PDB for `source=pdb`, AlphaFold DB for `source=alphafold`, and UniProt only when its API contributed input data.
- Cite optional scientific tools only when their node/artifact appears: Boltz-2, MODELLER, PLIP, FASPR, modXNA, GLYCAM, OL15/OL3, phosaa, PACKMOL-Memgen, etc.
- Do not cite diagnostics, warnings, failed attempts, or unused fallback paths in a Methods draft unless they affected the final protocol.

## Citation Targets by MDClaw Component

### Core MD And Topology

- OpenMM: cite for MD integration, platforms, reporters, barostats, HMR-enabled simulations, and OpenMM fallback solvation.
- AmberTools: cite for `tleap`, `antechamber`, `parmchk2`, `pdb4amber`, `sqm`, and Amber topology generation.
- PACKMOL: cite when explicit solvent or mixed systems are assembled through PACKMOL or PACKMOL-Memgen.
- PACKMOL-Memgen: cite when `embed_in_membrane` or membrane-building workflows use it.
- MEMEMBED: cite when membrane-protein orientation through `memembed` is used.

### Force Fields, Water Models, And Parameters

- ff19SB: cite when `forcefield=ff19SB`.
- ff14SB: cite when `forcefield=ff14SB` or `ff14SBonlysc`.
- GAFF/GAFF2: cite Wang et al. 2004 for GAFF-family ligand parameterization. A separate peer-reviewed GAFF2 DOI was not confirmed; cite AmberTools plus the original GAFF paper when GAFF2 is used through AmberTools.
- AM1-BCC: cite when `antechamber`/`sqm` generated ligand charges with AM1-BCC.
- OPC: cite when `water_model=opc`.
- OPC3: cite when `water_model=opc3`.
- TIP3P: cite when `water_model=tip3p`.
- SPC/E: cite when `water_model=spce`.
- TIP4P-Ew: cite when `water_model=tip4pew`.
- GLYCAM06: cite when glycan handling uses `leaprc.GLYCAM_06j-1`.
- OL15: cite when DNA uses `leaprc.DNA.OL15`.
- OL3: cite when RNA uses `leaprc.RNA.OL3`.
- phosaa14SB/phosaa19SB: cite when SEP/TPO/PTR phosphorylation is retained or reintroduced and `leaprc.phosaa*` is loaded.
- Li/Merz or Joung-Cheatham ion parameters: cite when explicit ion parameter selection is central to the Methods. For OPC/OPC3-specific Li/Merz parameters, prefer the Amber ion-parameter paper with DOI `10.1021/acs.jcim.0c01390` if those files are used.

### Structure Preparation And Chemistry

- PDBFixer: no separate DOI-confirmed PDBFixer paper was found; cite the OpenMM paper for the software family and mention PDBFixer as an OpenMM-associated tool if needed.
- PDB2PQR: cite for structure protonation/PQR preparation, especially when `prepare_complex` used `pdb2pqr`.
- PROPKA3: cite for empirical pKa/protonation-state assignment.
- RDKit: cite the RDKit Zenodo concept DOI for cheminformatics operations.
- Gemmi: cite for mmCIF/PDB parsing and structural biology file handling.
- Dimorphite-DL: cite when small-molecule ionization state enumeration is used.
- PubChemPy: normally do not cite in MD Methods; record only in software provenance if needed.
- FASPR: cite when `create_mutated_structure` uses side-chain packing.
- modXNA: cite when modified nucleic acid parameters are generated.
- PLIP: cite when protein-ligand interaction profiling is used. Use PLIP 2021 if nucleic-acid interactions are part of the analysis.

### AI Prediction And Data Resources

- Boltz-2: cite when `boltz2_protein_from_seq` or Boltz-2 predictions are used.
- MODELLER: cite when `modeller_from_alignment` builds a comparative model from a template PDB plus a target sequence or alignment.
- AlphaFold DB: cite the AlphaFold DB paper when a structure is fetched from AlphaFold DB, and cite the AlphaFold Nature paper if describing AlphaFold model provenance.
- RCSB PDB: cite the RCSB PDB resource paper when PDB files or metadata are fetched. Also cite individual PDB entry DOIs where journal style requires it.
- UniProt: cite when UniProt metadata, sequence, or accession mapping is used.
- PubChem: normally do not cite in MD Methods; record accessions or URLs in provenance if needed.

### Analysis And Infrastructure

- MDTraj: cite when MDClaw trajectory analysis nodes use MDTraj-derived calculations.
- NumPy: do not cite in Methods by default.
- PyTorch: do not cite in Methods by default; cite Boltz-2 rather than its runtime backend.
- SLURM: do not cite in Methods by default.
- Singularity/Apptainer: do not cite in Methods by default; record container use only in reproducibility notes if needed.
- Docker/CUDA/GHCR: normally record versions in reproducibility notes, but no DOI-based paper citation is included by default.
- ParmEd: MDClaw lists it as a dependency and future topology-manipulation helper; no dedicated DOI-confirmed citation was found in this pass.
- NCBI E-utilities/PubMed: cite NCBI/PubMed resource guidance if the literature-server workflow itself is described, but no DOI-based BibTeX entry is included below.

## DOI-Containing BibTeX Library

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

@article{Eastman2013OpenMM4,
  author = {Eastman, Peter and Friedrichs, Mark S. and Chodera, John D. and Radmer, Randall J. and Bruns, Christopher M. and Ku, Joy P. and Beauchamp, Kyle A. and Lane, Thomas J. and Wang, Lee-Ping and Shukla, Diwakar and Tye, Tony and Houston, Mike and Stich, Thomas and Klein, Christoph and Shirts, Michael R. and Pande, Vijay S.},
  title = {{OpenMM} 4: A Reusable, Extensible, Hardware Independent Library for High Performance Molecular Simulation},
  journal = {Journal of Chemical Theory and Computation},
  year = {2013},
  volume = {9},
  number = {1},
  pages = {461--469},
  doi = {10.1021/ct300857j}
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

@article{Martinez2009Packmol,
  author = {Martínez, Leandro and Andrade, Ricardo and Birgin, Ernesto G. and Martínez, José Mario},
  title = {{PACKMOL}: A Package for Building Initial Configurations for Molecular Dynamics Simulations},
  journal = {Journal of Computational Chemistry},
  year = {2009},
  volume = {30},
  number = {13},
  pages = {2157--2164},
  doi = {10.1002/jcc.21224}
}

@article{SchottVerdugo2019PackmolMemgen,
  author = {Schott-Verdugo, Stephan and Gohlke, Holger},
  title = {{PACKMOL-Memgen}: A Simple-To-Use, Generalized Workflow for Membrane-Protein-Lipid-Bilayer System Building},
  journal = {Journal of Chemical Information and Modeling},
  year = {2019},
  volume = {59},
  number = {6},
  pages = {2522--2528},
  doi = {10.1021/acs.jcim.9b00269}
}

@article{Nugent2013Memembed,
  author = {Nugent, Tim and Jones, David T.},
  title = {Membrane Protein Orientation and Refinement Using a Knowledge-Based Statistical Potential},
  journal = {BMC Bioinformatics},
  year = {2013},
  volume = {14},
  pages = {276},
  doi = {10.1186/1471-2105-14-276}
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

@article{Maier2015ff14SB,
  author = {Maier, James A. and Martinez, Carmenza and Kasavajhala, Koushik and Wickstrom, Lauren and Hauser, Kevin E. and Simmerling, Carlos},
  title = {{ff14SB}: Improving the Accuracy of Protein Side Chain and Backbone Parameters from {ff99SB}},
  journal = {Journal of Chemical Theory and Computation},
  year = {2015},
  volume = {11},
  number = {8},
  pages = {3696--3713},
  doi = {10.1021/acs.jctc.5b00255}
}

@article{Wang2004GAFF,
  author = {Wang, Junmei and Wolf, Romain M. and Caldwell, James W. and Kollman, Peter A. and Case, David A.},
  title = {Development and Testing of a General {AMBER} Force Field},
  journal = {Journal of Computational Chemistry},
  year = {2004},
  volume = {25},
  number = {9},
  pages = {1157--1174},
  doi = {10.1002/jcc.20145}
}

@article{Jakalian2002AM1BCC,
  author = {Jakalian, Araz and Jack, David B. and Bayly, Christopher I.},
  title = {Fast, Efficient Generation of High-Quality Atomic Charges. {AM1-BCC} Model: {II}. Parameterization and Validation},
  journal = {Journal of Computational Chemistry},
  year = {2002},
  volume = {23},
  number = {16},
  pages = {1623--1641},
  doi = {10.1002/jcc.10128}
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

@article{Izadi2016OPC3,
  author = {Izadi, Saeed and Onufriev, Alexey V.},
  title = {Accuracy Limit of Rigid 3-Point Water Models},
  journal = {The Journal of Chemical Physics},
  year = {2016},
  volume = {145},
  number = {7},
  pages = {074501},
  doi = {10.1063/1.4960175}
}

@article{Jorgensen1983TIP3P,
  author = {Jorgensen, William L. and Chandrasekhar, Jayaraman and Madura, Jeffry D. and Impey, Roger W. and Klein, Michael L.},
  title = {Comparison of Simple Potential Functions for Simulating Liquid Water},
  journal = {The Journal of Chemical Physics},
  year = {1983},
  volume = {79},
  number = {2},
  pages = {926--935},
  doi = {10.1063/1.445869}
}

@article{Berendsen1987SPCE,
  author = {Berendsen, H. J. C. and Grigera, J. R. and Straatsma, T. P.},
  title = {The Missing Term in Effective Pair Potentials},
  journal = {The Journal of Physical Chemistry},
  year = {1987},
  volume = {91},
  number = {24},
  pages = {6269--6271},
  doi = {10.1021/j100308a038}
}

@article{Horn2004TIP4PEw,
  author = {Horn, Hans W. and Swope, William C. and Pitera, Jed W. and Madura, Jeffry D. and Dick, Thomas J. and Hura, Greg L. and Head-Gordon, Teresa},
  title = {Development of an Improved Four-Site Water Model for Biomolecular Simulations: {TIP4P-Ew}},
  journal = {The Journal of Chemical Physics},
  year = {2004},
  volume = {120},
  number = {20},
  pages = {9665--9678},
  doi = {10.1063/1.1683075}
}

@article{Kirschner2008GLYCAM06,
  author = {Kirschner, Karl N. and Yongye, Austin B. and Tschampel, Sarah M. and González-Outeiriño, Jorge and Daniels, Charlisa R. and Foley, Bethany L. and Woods, Robert J.},
  title = {{GLYCAM06}: A Generalizable Biomolecular Force Field. Carbohydrates},
  journal = {Journal of Computational Chemistry},
  year = {2008},
  volume = {29},
  number = {4},
  pages = {622--655},
  doi = {10.1002/jcc.20820}
}

@article{Zgarbova2015OL15,
  author = {Zgarbová, Marie and Šponer, Jiří and Otyepka, Michal and Cheatham, Thomas E. and Galindo-Murillo, Rodrigo and Jurečka, Petr},
  title = {Refinement of the Sugar-Phosphate Backbone Torsion Beta for the {AMBER} Force Fields Improves the Description of {Z-DNA} and {B-DNA}},
  journal = {Journal of Chemical Theory and Computation},
  year = {2015},
  volume = {11},
  number = {12},
  pages = {5723--5736},
  doi = {10.1021/acs.jctc.5b00716}
}

@article{Zgarbova2011OL3,
  author = {Zgarbová, Marie and Otyepka, Michal and Šponer, Jiří and Mládek, Aleš and Banáš, Pavel and Cheatham, Thomas E. and Jurečka, Petr},
  title = {Refinement of the {Cornell} et al. Nucleic Acids Force Field Based on Reference Quantum Chemical Calculations of Glycosidic Torsion Profiles},
  journal = {Journal of Chemical Theory and Computation},
  year = {2011},
  volume = {7},
  number = {9},
  pages = {2886--2902},
  doi = {10.1021/ct200162x}
}

@article{Homeyer2006PhosphoAA,
  author = {Homeyer, Nadine and Horn, Anselm H. C. and Lanig, Harald and Sticht, Heinrich},
  title = {{AMBER} Force-Field Parameters for Phosphorylated Amino Acids in Different Protonation States: Phosphoserine, Phosphothreonine, Phosphotyrosine, and Phosphohistidine},
  journal = {Journal of Molecular Modeling},
  year = {2006},
  volume = {12},
  number = {3},
  pages = {281--289},
  doi = {10.1007/s00894-005-0028-4}
}

@article{Raguette2024Phosaa,
  author = {Raguette, Lauren E. and Cuomo, Abbigayle E. and Belfon, Kellon A. A. and Tian, Chuan and Hazoglou, Victoria and Witek, Gabriela and Telehany, Stephen M. and Wu, Qin and Simmerling, Carlos},
  title = {{phosaa14SB} and {phosaa19SB}: Updated {Amber} Force Field Parameters for Phosphorylated Amino Acids},
  journal = {Journal of Chemical Theory and Computation},
  year = {2024},
  doi = {10.1021/acs.jctc.4c00732}
}

@article{Joung2008IonParameters,
  author = {Joung, In Suk and Cheatham, Thomas E.},
  title = {Determination of Alkali and Halide Monovalent Ion Parameters for Use in Explicitly Solvated Biomolecular Simulations},
  journal = {The Journal of Physical Chemistry B},
  year = {2008},
  volume = {112},
  number = {30},
  pages = {9020--9041},
  doi = {10.1021/jp8001614}
}

@article{Li2015HighlyChargedIons,
  author = {Li, Pengfei and Merz, Kenneth M.},
  title = {Taking into Account the Ion-Induced Dipole Interaction in the Nonbonded Model of Ions},
  journal = {Journal of Chemical Theory and Computation},
  year = {2014},
  volume = {10},
  number = {1},
  pages = {289--297},
  doi = {10.1021/ct400751u}
}

@article{Li2021IonParameters,
  author = {Sengupta, Arkajyoti and Li, Zhen and Song, Lin Frank and Li, Pengfei and Merz, Kenneth M. Jr.},
  title = {Parameterization of Monovalent Ions for the {OPC3}, {OPC}, {TIP3P-FB}, and {TIP4P-FB} Water Models},
  journal = {Journal of Chemical Information and Modeling},
  year = {2021},
  volume = {61},
  number = {2},
  pages = {869--880},
  doi = {10.1021/acs.jcim.0c01390}
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

@article{Jurrus2018APBSPDB2PQR,
  author = {Jurrus, Elizabeth and Engel, Dave and Star, Keith and Monson, Kyle and Brandi, Juan and Felberg, Lisa E. and Brookes, David H. and Wilson, Leighton and Chen, Jiahui and Liles, Kathryn and Chun, Melissa and Li, Peter and Gohara, David W. and Dolinsky, Todd and Konecny, Robert and Koes, David R. and Nielsen, Jens E. and Head-Gordon, Teresa and Geng, Weihua and Krasny, Robert and Wei, Guo-Wei and Holst, Michael J. and McCammon, J. Andrew and Baker, Nathan A.},
  title = {Improvements to the {APBS} Biomolecular Solvation Software Suite},
  journal = {Protein Science},
  year = {2018},
  volume = {27},
  number = {1},
  pages = {112--128},
  doi = {10.1002/pro.3280}
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

@software{RDKitZenodo,
  author = {{RDKit Contributors}},
  title = {{RDKit}: Open-Source Cheminformatics},
  year = {2024},
  doi = {10.5281/zenodo.591637},
  url = {https://www.rdkit.org}
}

@article{Wojdyr2022Gemmi,
  author = {Wojdyr, Marcin},
  title = {{GEMMI}: A Library for Structural Biology},
  journal = {Journal of Open Source Software},
  year = {2022},
  volume = {7},
  number = {73},
  pages = {4200},
  doi = {10.21105/joss.04200}
}

@article{Ropp2019DimorphiteDL,
  author = {Ropp, Patrick J. and Kaminsky, Jesse C. and Yablonski, Sara and Durrant, Jacob D.},
  title = {{Dimorphite-DL}: An Open-Source Program for Enumerating the Ionization States of Drug-Like Small Molecules},
  journal = {Journal of Cheminformatics},
  year = {2019},
  volume = {11},
  pages = {14},
  doi = {10.1186/s13321-019-0336-9}
}

@article{Huang2020FASPR,
  author = {Huang, Xiaoqiang and Pearce, Robin and Zhang, Yang},
  title = {{FASPR}: An Open-Source Tool for Fast and Accurate Protein Side-Chain Packing},
  journal = {Bioinformatics},
  year = {2020},
  volume = {36},
  number = {12},
  pages = {3758--3765},
  doi = {10.1093/bioinformatics/btaa234}
}

@article{Love2024modXNA,
  author = {Love, Owen and Galindo-Murillo, Rodrigo and Roe, Daniel R. and Dans, Pablo D. and Cheatham, Thomas E. and Bergonzo, Christina},
  title = {{modXNA}: A Modular Approach to Parameterization of Modified Nucleic Acids for Use with {Amber} Force Fields},
  journal = {Journal of Chemical Theory and Computation},
  year = {2024},
  volume = {20},
  number = {21},
  pages = {9354--9363},
  doi = {10.1021/acs.jctc.4c01164}
}

@article{Salentin2015PLIP,
  author = {Salentin, Sebastian and Schreiber, Sven and Haupt, V. Joachim and Adasme, Melissa F. and Schroeder, Michael},
  title = {{PLIP}: Fully Automated Protein-Ligand Interaction Profiler},
  journal = {Nucleic Acids Research},
  year = {2015},
  volume = {43},
  number = {W1},
  pages = {W443--W447},
  doi = {10.1093/nar/gkv315}
}

@article{Adasme2021PLIP,
  author = {Adasme, Melissa F. and Linnemann, Katja L. and Bolz, Sarah N. and Kaiser, Florian and Salentin, Sebastian and Haupt, V. Joachim and Schroeder, Michael},
  title = {{PLIP} 2021: Expanding the Scope of the Protein-Ligand Interaction Profiler to {DNA} and {RNA}},
  journal = {Nucleic Acids Research},
  year = {2021},
  volume = {49},
  number = {W1},
  pages = {W530--W534},
  doi = {10.1093/nar/gkab294}
}

@article{Passaro2025Boltz2,
  author = {Passaro, Saro and Corso, Gabriele and Wohlwend, Jeremy and Reveiz, Mateo and Thaler, Stephan and Somnath, Vignesh Ram and Getz, Noah and Portnoi, Tally and Roy, Julien and Stark, Hannes and Kwabi-Addo, David and Beaini, Dominique and Jaakkola, Tommi and Barzilay, Regina},
  title = {{Boltz-2}: Towards Accurate and Efficient Binding Affinity Prediction},
  journal = {bioRxiv},
  year = {2025},
  doi = {10.1101/2025.06.14.659707}
}

@article{Sali1993MODELLER,
  author = {Šali, Andrej and Blundell, Tom L.},
  title = {Comparative Protein Modelling by Satisfaction of Spatial Restraints},
  journal = {Journal of Molecular Biology},
  year = {1993},
  volume = {234},
  number = {3},
  pages = {779--815},
  doi = {10.1006/jmbi.1993.1626}
}

@article{Jumper2021AlphaFold,
  author = {Jumper, John and Evans, Richard and Pritzel, Alexander and Green, Tim and Figurnov, Michael and Ronneberger, Olaf and Tunyasuvunakool, Kathryn and Bates, Russ and Žídek, Augustin and Potapenko, Anna and Bridgland, Alex and Meyer, Clemens and Kohl, Simon A. A. and Ballard, Andrew J. and Cowie, Andrew and Romera-Paredes, Bernardino and Nikolov, Stanislav and Jain, Rishub and Adler, Jonas and Back, Trevor and Petersen, Stig and Reiman, David and Clancy, Ellen and Zielinski, Michal and Steinegger, Martin and Pacholska, Michalina and Berghammer, Tamas and Bodenstein, Sebastian and Silver, David and Vinyals, Oriol and Senior, Andrew W. and Kavukcuoglu, Koray and Kohli, Pushmeet and Hassabis, Demis},
  title = {Highly Accurate Protein Structure Prediction with {AlphaFold}},
  journal = {Nature},
  year = {2021},
  volume = {596},
  number = {7873},
  pages = {583--589},
  doi = {10.1038/s41586-021-03819-2}
}

@article{Varadi2022AlphaFoldDB,
  author = {Varadi, Mihaly and Anyango, Stephen and Deshpande, Mandar and Nair, Sreenath and Natassia, Cindy and Yordanova, Genoveva and Yuan, David and Stroe, Oana and Wood, Gemma and Laydon, Agata and Zídek, Augustin and Green, Tim and Tunyasuvunakool, Kathryn and Petersen, Stig and Jumper, John and Clancy, Ellen and Green, Richard and Vora, Amoolya and Lutfi, Mira and Figurnov, Michael and Cowie, Andrew and Hobbs, Nicole and Kohli, Pushmeet and Kleywegt, Gerard and Birney, Ewan and Hassabis, Demis and Velankar, Sameer},
  title = {{AlphaFold} Protein Structure Database: Massively Expanding the Structural Coverage of Protein-Sequence Space with High-Accuracy Models},
  journal = {Nucleic Acids Research},
  year = {2022},
  volume = {50},
  number = {D1},
  pages = {D439--D444},
  doi = {10.1093/nar/gkab1061}
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

@article{UniProt2025,
  author = {{The UniProt Consortium}},
  title = {{UniProt}: The Universal Protein Knowledgebase in 2025},
  journal = {Nucleic Acids Research},
  year = {2025},
  volume = {53},
  number = {D1},
  pages = {D609--D617},
  doi = {10.1093/nar/gkae1010}
}

@article{McGibbon2015MDTraj,
  author = {McGibbon, Robert T. and Beauchamp, Kyle A. and Harrigan, Matthew P. and Klein, Christoph and Swails, Jason M. and Hernández, Carlos X. and Schwantes, Christian R. and Wang, Lee-Ping and Lane, Thomas J. and Pande, Vijay S.},
  title = {{MDTraj}: A Modern Open Library for the Analysis of Molecular Dynamics Trajectories},
  journal = {Biophysical Journal},
  year = {2015},
  volume = {109},
  number = {8},
  pages = {1528--1532},
  doi = {10.1016/j.bpj.2015.08.015}
}

```

## Items Without A Dedicated DOI-Confirmed BibTeX Entry In This Pass

- PDBFixer: cite OpenMM 4 or OpenMM 8 depending on context; PDBFixer itself is best described as an OpenMM-associated preparation tool.
- GAFF2: no separate peer-reviewed GAFF2 DOI was confirmed; cite AmberTools and the original GAFF paper.
- ParmEd: no dedicated DOI-confirmed citation was found; cite documentation or repository only if journal style permits non-DOI software references.
- SLURM, PyTorch, Singularity/Apptainer, NumPy, PubChem, and PubChemPy: record versions, accessions, or URLs for provenance when useful, but do not include them as default Methods citations.
- Docker, GHCR, CUDA, NVRTC, `requests`, `httpx`, `pydantic`, `PyYAML`, and `python-dotenv`: record versions for reproducibility rather than citing in Methods by default.
- NCBI E-utilities/PubMed: use NCBI documentation or PubMed citation guidance if the literature-search tool itself is described, but do not include by default in MD simulation Methods.
