# Reviewer/reporter bibliography audit — 2026-09-06

## Scope and verification level

Research inventory; `generate_md_report` now implements a limited, evidence-based
selector, not every mapping in this inventory (see the developer tool reference).
The accompanying
[BibTeX library](citation-audit-2026-09-06.bib) contains 111 records: 109 DOI records
checked against publisher-deposited Crossref metadata, the H-Packer proceedings
record checked at PMLR, and an AshGC working paper checked at Zenodo. Bibliographic
identity is not evidence that a particular MD run used a method. Not every article
was read in full; manual sections and official citation instructions were used to
establish the component mappings below. Unresolved mappings remain explicit.

The old inventory was read from `a9c6255^:mdclaw/evidence/citation_inventory.md`
(original reporting work: `c519b8d`; removed in `a9c6255`). Its 38 non-Zenodo DOI
records were checked, not copied as authoritative. The old RDKit entry was not
retained as a fictitious fixed-year release; H-Packer was independently verified.
Current implementation inspected at `dc974d3` plus existing uncommitted prep fixes.
No simulation or CLI behavior changed in this audit.

## Primary sources and maintenance policy

- [Amber 2026 Reference Manual](https://ambermd.org/doc12/Amber26.pdf), official
  [manual index](https://ambermd.org/Manuals.php): edition updated 2026-06-22.
  Download SHA256: `7f12b0c947685899eac077e632b1c8b234238047ea7ceb9b9c6fd8452ec66778`.
  Chapter/section and reference numbers below refer to this edition, not Amber25.
- [Amber citation instructions](https://ambermd.org/CiteAmber.php).
- [OpenMM citation instructions](https://docs.openmm.org/latest/userguide/introduction.html#referencing-openmm)
  and [author-posted OpenMM 8 manuscript](https://arxiv.org/abs/2310.03121).
- [PACKMOL citation instructions](https://m3g.github.io/packmol/citation.shtml);
  PACKMOL-Memgen is explicitly identified in Amber manual section 13.8.
- [OpenFF citation instructions](https://openforcefield.org/science/how-to-cite/),
  [GAFF template-generator documentation](https://github.com/openmm/openmmforcefields),
  and the [exact NAGL model card](https://github.com/openforcefield/openff-nagl-models/blob/main/docs/models/openff-gnn-am1bcc-1.0.0/index.md).
- [PLUMED citation instructions](https://www.plumed.org/cite),
  [PROPKA citation instructions](https://github.com/jensengroup/propka#references--citations),
  [PDB2PQR citation instructions](https://pdb2pqr.readthedocs.io/en/latest/supporting.html),
  [MDTraj's official BibTeX](https://mdtraj.org/1.9.4/index.html#citation).

Each DOI-backed BibTeX record includes its exact Crossref metadata endpoint in a
comment. Crossref is used as the publisher-deposited bibliographic record, not as
a substitute for scientific evidence of applicability. A successful DOI lookup
alone is insufficient: title, author list, venue, year, volume and pages must agree.
Use issue/print year where available; preserve initials as supplied rather than
guessing full names. Online and issue dates can legitimately differ (OpenMM 8:
2023 online / 2024 issue; ff19SB: 2019 online / 2020 issue).

## Confirmed corrections and omissions in the old inventory

1. **GAFF DOI pointed to an erratum.** `10.1002/jcc.20145` describes the correction
   in volume 26, page 114 (2005), not the original article. The original is
   [10.1002/jcc.20035](https://onlinelibrary.wiley.com/doi/10.1002/jcc.20035),
   volume 25, 1157–1174 (2004). The new library uses the original DOI.
2. **OpenMM 8 author list was corrupted.** Examples: Charlles R. A. Abreu, Joshua
   A. Mitchell, Sukrit Singh, Philip Turner, Yuanqing Wang and Ivy Zhang were
   incorrectly expanded/replaced; Vijay S. Pande was missing. Rebuilt from the
   publisher record and checked against the author-posted manuscript. AmberTools
   also contained incorrect expansions of author names; no guessed expansions
   from the old list were retained.
3. **PDB2PQR title was incorrect.** The 2004 title is “PDB2PQR: an automated
   pipeline for the setup of Poisson-Boltzmann electrostatics calculations”, not
   a title containing “setup, execution, and analysis”. The modXNA title uses
   “Parametrization”, not the old “Parameterization”.
   PDB2PQR's own citation page also uses the longer title: official citation
   instructions identify what to cite, but publisher records settle exact titles.
4. **OL15 was represented by only its beta-torsion refinement.** Amber section
   3.2.2, table 3.2 links OL15 to reference [67], Galindo-Murillo et al. (2016).
   Added that record. OL15 combines multiple refinements; the 2015 beta paper
   alone must not be described as the complete OL15 provenance.
5. **Lipid21, HMR, CPPTRAJ, Antechamber, PLUMED and equilibration detection were
   missing as explicit BibTeX records.** Added primary method/software records.
   Added the second PROPKA reference recommended by its maintainers and the
   PACKMOL packing-strategy paper (conditional on describing that strategy).
6. **Ion models were conflated.** The OPC-family 2021 monovalent-ion paper is not
   the 2020 divalent-ion paper. A 12-6-4 paper must not be used to claim a plain
   12-6 runtime System implements the extra polarization term. Corrected the
   misleading `Li2015HighlyChargedIons` key below to `Li2014IonInducedDipole`.
7. **Charge execution was misdescribed.** Current `_assign_nagl_partial_charges`
   records NAGL success or AM1-BCC fallback per ligand; the old statement that
   curated workflows never invoke AM1-BCC is false. Conversely, NAGL predicts
   AM1-BCC-like charges; it does not mean QM AM1-BCC was run on that ligand.
   GAFF atom typing/parameter generation is distinct from charge fitting.
8. **A software citation need not be a journal article.** PDBFixer, Pablo,
   openmmforcefields and plugin versions should retain their own software identity;
   a related OpenMM/OpenFF paper is not a dedicated paper for each package.

## Component-to-reference mapping

This is a curated selection specification, not substring-matching rules. Entries
below are selected only with positive evidence in the target lineage's successful
execution records or resolved artifacts. Dependency installation, a failed attempt,
or a warning mentioning a package does not establish its use.

### Non-Amber software and workflows

| Component / actual use | BibTeX keys | Primary applicability evidence / qualification |
| --- | --- | --- |
| OpenMM simulation/System operations | `Eastman2024OpenMM8` | OpenMM official citation instruction; actual engine version still recorded separately |
| Historical OpenMM implementation discussion | `Eastman2013OpenMM4` | Historical reference only; not an extra mandatory citation for every modern run |
| PACKMOL assembly | `Martinez2009Packmol` | PACKMOL official instructions and original Wiley paper |
| PACKMOL packing algorithm described | `Martinez2003Packing` | Explicit conditional recommendation on PACKMOL citation page |
| PACKMOL-Memgen membrane building | `SchottVerdugo2019PackmolMemgen` + PACKMOL | Amber 13.8, [446]; orientation/packing subtools depend on what actually ran |
| MEMEMBED orientation | `Nugent2013Memembed` | Amber 13.8, [442]; original BMC Bioinformatics article |
| OpenFF Toolkit | `Mobley2018OpenFF` + actual software release | Official OpenFF software citation instruction; does not imply using Sage/SMIRNOFF parameters |
| NAGL Ash 1.0 charges | `Wang2025AshGCWorkingPaper` + model/software record | Exact model card links this working paper; see unresolved 2026-paper mapping below |
| PDB2PQR | `Dolinsky2004PDB2PQR`, `Jurrus2018APBSPDB2PQR` | PDB2PQR official citation page; citing APBS-suite paper does not mean APBS was executed |
| PROPKA | `Olsson2011PROPKA3`, `Sondergaard2011PROPKA` | Both recommended in official PROPKA README |
| Dimorphite-DL ligand protonation | `Ropp2019DimorphiteDL` | Original Journal of Cheminformatics article, DOI 10.1186/s13321-019-0336-9 |
| Gemmi structure parsing | `Wojdyr2022Gemmi` | JOSS primary record, DOI 10.21105/joss.04200 |
| H-Packer side-chain reconstruction | `Visani2024HPacker` | PMLR volume 240, 230–249; official BibTeX, no invented DOI |
| MODELLER comparative modeling | `Sali1993MODELLER` | Primary JMB article identified by DOI; select only if modeling contributed to target structure |
| PLIP interactions | `Salentin2015PLIP`, `Adasme2021PLIP` | Original NAR records; identify actual software/analysis version and nucleic-acid use |
| MDTraj analysis or trajectory export | `McGibbon2015MDTraj` | Official MDTraj BibTeX; export use is a separate reporting/deposition step |
| Automatic equilibration detection | `Chodera2016Equilibration` | Original paper + PyMBAR timeseries documentation; do not label this MBAR free-energy estimation |
| PLUMED 2 | `Tribello2014PLUMED2`, `PLUMED2019Consortium` | Official PLUMED citation instructions require base references plus actually used feature references |
| TorchForce/openmm-torch | `Eastman2024OpenMM8` + plugin/software record | OpenMM 8 describes PyTorch coupling; plugin alone cannot identify an arbitrary custom potential's literature |

### Amber force fields and method components

| Actual parameter/method selection | BibTeX keys | Amber26 evidence / selection boundary |
| --- | --- | --- |
| AmberTools preparation/parameter generation | `Case2023AmberTools` | Official citation page; do not infer pmemd/sander execution |
| Antechamber atom/bond typing | `Wang2006Antechamber` | [456], chapter 16; independent of whether charges were supplied |
| CPPTRAJ, including preparation use | `Roe2013Cpptraj` | Tool-specific reference, DOI 10.1021/ct400341p; require actual execution |
| ff19SB | `Tian2020ff19SB` | 3.1.1, [22] |
| ff14SB / ff14SBonlysc | `Maier2015ff14SB` | 3.1.1, [24]; onlysc variant must not be described as full ff14SB |
| ff15ipq | `Debiec2016ff15ipq` | 3.1, [29] |
| AMBER-FB15 | `Wang2017FB15` | 3.1, [37]; associated ForceBalance water citation still needed |
| GAFF / GAFF2 family | `Wang2004GAFF`, `Case2023AmberTools` | [455]; record exact version (current curated builder: gaff-2.11), not an invented standalone GAFF2 paper |
| Actual AM1-BCC calculation | `Jakalian2000AM1BCC`, `Jakalian2002AM1BCC` | [457–458]; require actual successful fitting/fallback evidence |
| OPC | `Izadi2014OPC` | [23], water-model section |
| OPC3 | `Izadi2016OPC3` | [115], water-model section |
| TIP3P | `Jorgensen1983TIP3P` | [109]; model variant must match loaded XML |
| SPC/E | `Berendsen1987SPCE` | Water-model section; DOI 10.1021/j100308a038 |
| TIP4P-Ew | `Horn2004TIP4PEw` | Water-model section; DOI 10.1063/1.1683075 |
| GLYCAM06 | `Kirschner2008GLYCAM06` | 3.4, [81]; record GLYCAM file/version |
| DNA OL15 | `GalindoMurillo2016OL15`, `Zgarbova2015OL15` | 3.2.2/table 3.2, [66–67]; constituent alpha/gamma, chi, epsilon/zeta references are follow-up items |
| RNA OL3 | `Zgarbova2011OL3` | 3.2, [54]; backbone/base-force-field provenance remains distinct |
| phosaa14SB/phosaa19SB | `Raguette2024Phosaa` | 3.3.1, [77]; volume 20/pages 7199–7209 supplied by manual because Crossref record lacks them |
| Older phosphorylated-AA parameters | `Homeyer2006PhosphoAA` | 3.3.1, [76]; not a substitute for new phosaa14SB/19SB |
| modXNA | `Love2024modXNA` | 3.3, [80]; require actual parameter-generation provenance |
| Lipid21 | `Dickson2022Lipid21` | Lipid section, [107]; also author-maintained github.com/callumjd/lipid21 |
| Joung–Cheatham monovalent ions | `Joung2008IonParameters` | [143]; identify actual water-specific parameter files |
| OPC-family monovalent ions | `Li2021IonParameters` | [136]; first author Sengupta (legacy key retained); not divalent parameters |
| OPC-family divalent ions | `Li2020DivalentIons` | [135]; preserve 12-6/HFE/IOD/12-6-4 variant identity |
| Explicit ion-induced dipole 12-6-4 model | `Li2014IonInducedDipole` | [145]; only if actual runtime interaction includes this model |
| Hydrogen mass repartitioning | `Hopkins2015HMR` | [142]; verify modified masses, not merely a 4 fs timestep |
| PME / smooth PME | `Darden1993PME`, `Essmann1995SmoothPME` | PME primary records; selection by actual nonbonded method, not periodic box alone |
| LangevinMiddleIntegrator | `Zhang2019LFMiddle` | OpenMM API identifies JPCA 123, 6056–6079; publisher DOI 10.1021/acs.jpca.9b02771 |

### Input models and databases

These records have verified DOI bibliographic identities but must follow the
actual source artifact, not merely the job's current description.

| Source | BibTeX keys | Qualification |
| --- | --- | --- |
| Boltz-2 prediction | `Passaro2025Boltz2` | bioRxiv preprint, explicitly labeled; does not imply an affinity calculation was run |
| AlphaFold model | `Jumper2021AlphaFold` | Model provenance, not a claim to have rerun AlphaFold |
| AlphaFold DB retrieval | `Varadi2022AlphaFoldDB` | Verified historical resource paper; record accession/retrieval and applicable DB release |
| RCSB PDB retrieval | `Burley2025RCSBPDB` | Add entry-specific primary structural publication/accession when known; not guessed |
| UniProt retrieval/mapping | `UniProt2025` | Only if sequence/metadata mapping used it |

## Software identities / unresolved or version-dependent references

Do not fabricate a dedicated article or resolve a release by choosing today's
latest version. Software references can use a versioned repository/release URL;
DOIs are not mandatory. These software-release identities are not resolved by the
111-record paper library.

- [PDBFixer](https://github.com/openmm/pdbfixer),
  [openmmforcefields](https://github.com/openmm/openmmforcefields),
  [OpenFF Pablo](https://github.com/openforcefield/openff-pablo): dedicated
  peer-reviewed article not confirmed in this pass. Record exact software version
  and repository, plus related engine/toolkit paper where appropriate.
- [RDKit](https://github.com/rdkit/rdkit): concept DOI `10.5281/zenodo.591637`
  must not be paired with an arbitrary release year. Resolve the actual release
  citation during implementation/deposition; meanwhile use software URL/version.
- [OpenFF NAGL](https://github.com/openforcefield/openff-nagl) and
  [NAGL models](https://github.com/openforcefield/openff-nagl-models): preserve
  model name and hash. The exact `openff-gnn-am1bcc-1.0.0.pt` card links the 2025
  working paper. `Wang2026AshGC` is bibliographically verified and recommended
  by OpenFF for Sage 2.3.0, but its exact mapping to this GAFF + Ash-model workflow
  needs paper/model-version review before automatic selection. Never describe
  GAFF + NAGL as Sage merely because this paper's title contains Sage.
- [openmm-torch](https://github.com/openmm/openmm-torch) and
  [openmm-plumed](https://github.com/openmm/openmm-plumed): separate plugin
  version/build identity from engine/PLUMED papers. Custom TorchForce/CV methods
  require author-provided method provenance; unsupported references stay unknown.
- PLUMED requests action-specific citations from its actual log. A fixed base
  catalog cannot establish completeness for arbitrary scripts; unresolved log
  references must be exposed, not dropped or invented by the LLM.
- The second pass below adds references for legacy ff03/ff99 variants,
  ForceBalance waters, alternative DNA/RNA parameters and implicit solvent.
  Remaining constituent/release mappings are listed explicitly; extra XML still
  requires its own provenance. Do not substitute the nearest common paper.
- Constraint algorithms, membrane-barostat variants, arbitrary analysis plugins,
  umbrella/steering methods and custom potentials need implementation-specific
  selection. A harmonic restraint alone is not evidence of WHAM/MBAR analysis or
  a particular published steered-MD protocol. No such claims are auto-authorized.

## Second pass: code-driven coverage audit

The initial 59-record inventory missed databases and algorithms. This pass adds
40 DOI-verified records; it does **not** establish complete citations for arbitrary
external scripts or every installed package. The sweep used runtime imports and
calls, `forcefield_catalog.py`, analysis registration, and membrane-orientation
provenance, not just package names. Code anchors refer to this checkout on
2026-09-06; runtime files were not modified by this research.

### Databases, membrane orientation, and chemistry preparation

| Actual operation / code anchor | Citation keys | Selection boundary |
| --- | --- | --- |
| OPM homolog orientation: `solvation/membrane.py:898`, `solvation/opm_orient.py:1223` | `Lomize2006OPM`, `Lomize2012OPMPPM` | Successful OPM donor transfer; retain donor ID, URL, hash and alignment evidence |
| Local PPM3 fallback or explicit execution: `solvation/ppm_orient.py:36` | `Lomize2022PPM3`; `Lomize2006PPM` for underlying method | Actual `immers` execution, not merely an attempted fallback or OPM lookup |
| Explicit MEMEMBED orientation | Existing `Nugent2013Memembed` | Do not cite it for current `auto`, which tries OPM then PPM3 |
| CCD ligand chemistry lookup: `structure/ligand_chemistry.py:104` | `Westbrook2015CCD` | CCD used to recover chemical identity/bonding; separate from structure-dataset citation |
| ETKDGv3 embedding: `structure/ligand_chemistry.py:267` | `Riniker2015ETKDG`, `Wang2020ETKDG` | Only no-conformer embedding branch; retain actual options, not all optional ring algorithms |
| MMFF94 pre-optimization: `structure/clean_ligand.py:333` | `Halgren1996MMFF94`, `Tosco2014RDKitMMFF` | Optimization requested and actual MMFF branch executed |
| UFF fallback: `structure/ligand_chemistry.py:280` | `Rappe1992UFF` | Actual fallback, not every ligand preparation |

Sources: original [OPM paper](https://doi.org/10.1093/bioinformatics/btk023),
[OPM/PPM resource paper](https://doi.org/10.1093/nar/gkr703),
[PPM3 paper](https://doi.org/10.1002/pro.4219),
[wwPDB CCD citation guidance](https://west.wwpdb.org/data/ccd),
[RDKit method documentation](https://www.rdkit.org/docs/RDKit_Book.html), and
[RDKit MMFF implementation paper](https://doi.org/10.1186/s13321-014-0037-3).

OPM membrane placement is computationally estimated, with experimental comparison;
it is not a directly measured experimental membrane frame. The current source's
phrases "experimentally oriented" / "experimentally curated frame" must not be
copied into Methods as that stronger claim. Transferring an OPM donor frame is
also not evidence that PPM3 was run on the target. `orientation.method`,
`orientation.attempts`, and `parameters.orientation_backend_used` distinguish
attempts from the selected result.

The ligand helper returns only a success flag, and `clean_ligand` records
`optimization_converged`; neither alone identifies MMFF versus UFF fallback.
Existing logs may resolve that branch; otherwise report the method as unresolved.
Preparation MMFF/UFF must not be mislabeled as the subsequent MD force field.

### Analysis, free energies, and steering

| Actual operation / code anchor | Citation keys | Selection boundary |
| --- | --- | --- |
| Built-in trajectory I/O, fit, RMSD/RMSF/distance/contact analysis: `analyze/__init__.py` | Existing `McGibbon2015MDTraj` | MDTraj runtime use, not MDAnalysis by assumption |
| Optimally superposed MDTraj RMSD: `analyze/metrics.py:52` | `Theobald2005QCP` | QCP implementation; the local "Kabsch" comment is not sufficient algorithm evidence |
| In-house SVD fit: `solvation/opm_orient.py:580` | `Kabsch1976Superposition` | Actual SVD/Kabsch path; do not conflate with the QCP numerical implementation |
| Native-contact Q: `analyze/metrics.py:530` | `Best2013NativeContacts` | Best–Hummer–Eaton functional form; record actual selection, cutoff, beta, lambda and residue gap |
| Equilibration detection: `analyze/equilibration.py:74` | Existing `Chodera2016Equilibration`; `Chodera2007Timeseries` where applicable | `pymbar.timeseries.detect_equilibration`; this is not an MBAR free-energy estimate |
| External analysis using MDAnalysis | `MichaudAgrawal2011MDAnalysis`, `Gowers2016MDAnalysis` | Actual external-script provenance; dependency declaration is not execution |
| MDAnalysis QCP rotations | `Theobald2005QCP`, `Liu2010QCPRotation` | Specific algorithm use, in addition to the two library papers |
| External MBAR estimation | `Shirts2008MBAR` | Actual estimator, input reduced potentials, temperature/bias handling, and software version |
| External WHAM estimation | `Kumar1992WHAM`; implementation-dependent `Kumar1995WHAM`, `Roux1995PMF` | Actual estimator and implementation; not inferred from umbrella-window names |
| Umbrella sampling protocol | `Torrie1977Umbrella` | Biased-window sampling; estimator and convergence reported separately |
| Moving-target distance steering | `Isralewitz2001SteeredMD` as methodological context | Review/method article, not asserted to be the first SMD paper or an exact implemented protocol |

MDAnalysis is an explicit dependency (`pyproject.toml:36`), intended also for
external/adaptive analysis (`environment.yml:15`). No MDAnalysis import was found
in the built-in runtime paths. PyMBAR's only runtime import is the timeseries
module. No built-in MBAR or WHAM estimator was found. `register_analysis_result`
(`analyze/registry.py:62`) stores supplied method/provenance; it neither executes
that external analysis nor independently proves that the declaration is true.
Require the external script/version or hash, inputs, outputs and execution record
before calling such an analysis verified.

The [MDAnalysis citation guide](https://www.mdanalysis.org/citations/) requests
both library papers; its [algorithm-specific references](https://docs.mdanalysis.org/stable/documentation_pages/references.html)
also cover hydrogen bonds, diffusion/MSD, PCA overlap, etc. These remain conditional
extensions, not mandatory citations for every trajectory conversion. Duecredit
output, if available, is useful provenance but is not a required new framework.
For the 2016 paper, the citation landing page swaps Domański and Dotson relative
to Crossref and the [conference manuscript source](https://raw.githubusercontent.com/scipy-conference/scipy_proceedings/2016/papers/oliver_beckstein/oliver_beckstein.rst).
The BibTeX follows the manuscript's order and explicit names.

[PyMBAR's own citation guidance](https://github.com/choderalab/pymbar#references)
recommends the MBAR paper for software use and separately identifies timeseries
and equilibration methods. Thus a software-credit citation to the MBAR paper can
be appropriate even for timeseries use, but its selection reason must say
"PyMBAR software", never falsely "MBAR free-energy analysis". No separate
"PyMBAR 4 paper" was verified; preserve the actual software release identity.
[Grossfield WHAM's guidance](https://github.com/agrossfield/wham#wham) requests
the software title, URL and actual version in addition to theoretical background;
it explicitly states there is no dedicated software publication. Do not invent
a DOI or insert a guessed version into a publication-ready BibTeX record.

Other method sources: [MDTraj RMSD documentation](https://mdtraj.readthedocs.io/en/latest/api/generated/mdtraj.rmsd.html),
[native-contact example](https://www.mdtraj.org/1.3.0/examples/native-contact.html),
[umbrella original article](https://doi.org/10.1016/0021-9991(77)90121-8), and
[SMD author-hosted article](https://www.ks.uiuc.edu/Publications/Papers/PDF/ISRA2001A/ISRA2001A.pdf).
Do not add Jarzynski/Crooks/BAR, metadynamics, or a convergence claim merely
because a steering, TorchForce or PLUMED node exists. PLUMED action logs and
custom-method provenance determine those additional citations.

### Expanded force-field / solvent coverage

Amber26 sections 3.1–3.3 and the generalized-Born sections were cross-checked with
the [OpenMM model-to-reference table](https://docs.openmm.org/latest/userguide/application/02_running_sims.html#implicit-solvent)
and publisher metadata. Manual bibliographies can contain typographical errors;
the DOI records, not manual OCR, supply the final authors, issue and pages.

| Catalog option(s) | Added or existing citation keys | Remaining boundary |
| --- | --- | --- |
| ff99SB; ff14SBonlysc | `Hornak2006ff99SB`; add existing `Maier2015ff14SB` for onlysc | Record onlysc rather than full ff14SB backbone |
| ff99SBildn; ff99SBnmr | `LindorffLarsen2010ILDN`; `Li2010NMR`, plus base ff99SB | Select the actual refinement |
| ff03.r1; ff03ua | `Duan2003ff03`; `Yang2006ff03ua` | r1 terminal-library revision needs exact release provenance |
| tip3pfb; tip4pfb | `Wang2014ForceBalance` | Citing fitting method does not mean this run refit a force field |
| HCT; OBC1/OBC2; GBn; GBn2 | `Hawkins1995HCT`; `Onufriev2004OBC`; `Mongan2007GBn`; `Nguyen2013GBn2` | Retain XML, radii, dielectric and salt settings; SA term citations remain implementation-dependent |
| DNA bsc0; bsc1 | `Perez2007BSC0`; `Ivani2016BSC1` | Base-parameter ancestry must follow loaded files |
| DNA OL15 | `Perez2007BSC0`, `Krepl2012ChiOL4`, `Zgarbova2013EpsilonZeta`, existing beta and OL15 assessment papers | Added constituent papers; do not reduce OL15 to beta alone |
| DNA OL21 | OL15 constituents plus `Zgarbova2021OL21` | Not a license to report newer OL24 |
| RNA OL3; YIL; ROC | Existing `Zgarbova2011OL3`; `Yildirim2010YIL`; `Aytenfisu2017ROC` | bsc0 applies to OL3/YIL; preserve the selected family |
| ff19SB, ff14SB, ff15ipq, fb15; OPC/OPC3/TIP3P/SPCE/TIP4PEw; GLYCAM; GAFF | Initial-pass entries above | Exact release and combination still required |
| phosaa14SB/phosaa19SB; phosaa10 | `Raguette2024Phosaa`; `Homeyer2006PhosphoAA` + `Steinbrecher2012Phosphates` | phosaa10 charge and phosphate-oxygen vdW sources now resolved from shipped leaprc/XML |
| phosfb18 | `Stoppelman2021FB18`, `Stoppelman2022FB18Correction`, plus actual FB15 base | Corrected frcmod matches author file; 55 XML proper-torsion groups / 235 terms verified |
| lipid17 | Six distribution-declared references in the resolution note below | Lipid17 v1.1; retain parameter-release identity, not a fabricated standalone paper |
| lipid21 / lipid21_full | Existing `Dickson2022Lipid21` | Same family does not erase modular versus whole-lipid template identity |
| ff94/ff96/ff99 obsolete entries; arbitrary extra XML | **Not covered for automatic citation** | Catalog marks these old protein entries obsolete; historical/external inputs need separate review |

This is now a mapped research inventory, not a claim that every force-field
constituent is closed. Remaining work before a production citation selector:
base/release ancestry beyond the three resolved families below,
ion-parameter applicability, NAGL model mapping, implicit-solvent surface terms,
and per-run constraint-solver identification (see the OpenMM mapping below). For example, do not
automatically report SHAKE for OpenMM constraints or a Berendsen thermostat for
`LangevinMiddleIntegrator`. Preserve the real classes/settings and consult the
actual OpenMM build. NumPy and other utility dependencies are not a reason to
invent extra scientific methods; software acknowledgments can be a separate layer.

### Third pass: Lipid17 / phosfb18 / phosaa10 gaps resolved

[Detailed resolution and artifact hashes](lipid17-phosfb18-citation-resolution.md)
closes these three named citation mappings using the installed openmmforcefields
0.16.0 XML, Amber leaprc/parameter files, the fixed author repository and publisher
metadata. Nine records were added. Lipid17's six shipped references are preserved
as distribution-declared citations, separate from claims about cholesterol or
self-assembly in a particular run. FB18's original DOI is `1c07547`, not the
unrelated `1c10971` linked by the author's README; its 2022 correction is included.
The local frcmod is byte-identical to the author's corrected file, and the XML's
proper-torsion terms match it. This supersedes the earlier unresolved status for
these families, not the separate per-job/runtime-provenance requirements.

### OpenMM: official citations versus implementation provenance

Checked the official website on 2026-09-06. These are distinct evidence roles,
not a requirement to attach a dedicated paper to every feature:

| Component | Bibliographic mapping | Evidence role |
|---|---|---|
| `MonteCarloBarostat` | `Chow1995MCBarostat`, `Aqvist2004MCBarostat` | Both explicitly cited in the official theory section; method foundations, not dedicated OpenMM implementation papers |
| `LangevinMiddleIntegrator` | `Zhang2019LFMiddle` | Main LF-middle method reference explicitly identified by the official API |
| BAOAB relationship | `Leimkuhler2016BAOAB` | Related-method reference in the same API; do not claim that geodesic integration or solvent–solute splitting was executed |
| `MonteCarloMembraneBarostat` | OpenMM software citation and version-specific documentation/source | No separate dedicated paper citation found in the checked official section; base MC references do not establish membrane-specific implementation details |
| SETTLE / SHAKE / CCMA constraints | Implementation-derived supplementary references, conditional on actual solver use | The checked Constraints section does not cite these papers or identify the selected solver; do not label them as manual-prescribed citations |

Official sources: [barostat theory](https://docs.openmm.org/latest/userguide/theory/02_standard_forces.html#montecarlobarostat),
[membrane barostat theory](https://docs.openmm.org/latest/userguide/theory/02_standard_forces.html#montecarlomembranebarostat),
[Langevin middle API](https://docs.openmm.org/latest/api-python/generated/openmm.openmm.LangevinMiddleIntegrator.html),
[Constraints](https://docs.openmm.org/latest/userguide/application/02_running_sims.html#constraints),
and [official bibliography](https://docs.openmm.org/latest/userguide/zbibliography.html).

The constraint candidates are SETTLE (10.1002/jcc.540130805), SHAKE
(10.1016/0021-9991(77)90098-5), and CCMA (10.1021/ct900463w).
These are not an unconditional per-run citation set. The inspected
[common-platform source at f7fa0c2](https://github.com/openmm/openmm/blob/f7fa0c2/platforms/common/src/IntegrationUtilities.cpp)
selects SETTLE, small SHAKE clusters, and CCMA according to constraint structure;
the existence of these paths does not establish which ran for a given System.
`HBonds` specifies bonds involving hydrogen, not hydrogen-bond interactions or
a solver name. Harmonic restraints are separate from these constraints.

Keep official citation, supplementary method citation, implementation source,
and actual execution evidence separate. A feature with no dedicated paper is
not a missing-citation error: retain the software reference, documentation URL,
version and settings. Do not invent a paper or borrow one from another algorithm.
The online `latest` documentation identifies 8.6, whereas the inspected local
SIF reported `8.5.1.dev-f7fa0c2`; do not transfer newer behavior to that runtime.
This resolves the citation-policy question. The report CLI automates only its
explicitly documented subset; unresolved mappings remain visible in its output.

## Required behavior for the future CLI/skill

1. Extract executed facts with node ID and source artifact/field. Distinguish
   declared conditions, resolved parameters, and actual runtime evidence.
2. Select a deduplicated verified citation set using those facts; return the
   selection reason and missing provenance. Do not infer execution from filenames
   or full-JSON substring searches.
3. Keep stage-specific methods and replica identities. For multiple terminal
   nodes, ask the user whether to combine reports, separate campaigns, or omit
   branches; do not choose automatically.
4. LLM writes explanation/Methods from facts and fixed BibTeX keys. It must not
   invent missing authors, DOIs, parameters, convergence or validation claims.
5. MDDB YAML follows the obtained official workflow template. Methods references
   are distinct from the YAML dataset/publication citation field. No further
  deposit-page URL is required from the user for the agreed initial scope.

## Deliverable checks

111 unique BibTeX keys, 110 unique DOIs, balanced field/entry braces, and
`git diff --check` passed. A BibTeX parser/TeX engine is not installed in the
available local Python environment, so no rendered bibliography build was tested.
