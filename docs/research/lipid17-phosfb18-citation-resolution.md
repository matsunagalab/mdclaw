# Lipid17 / phosfb18 / phosaa10 citation resolution — 2026-09-06

## Conclusion and scope

The three named parameter-family citation gaps are resolved for the inspected
distribution: local `mdclaw.sif`, openmmforcefields **0.16.0**. This establishes
which references belong to these parameter sets, not that a particular historical
job loaded them. Per-job selection must still use recorded files and versions.
Nine publisher-metadata-verified records were added to
[the bibliography](citation-audit-2026-09-06.bib), now 108 entries.

## Lipid17: released parameters, not an invented standalone journal article

The container's `dat/leap/parm/lipid17.dat` and
`dat/leap/cmd/oldff/leaprc.lipid17` explicitly identify **Lipid17 v1.1**.
The latter loads `lipid17.dat` and `oldff/lipid17.lib`. Its converted
`openmmforcefields/ffxml/amber/lipid17.xml` identifies the source as
`oldff/leaprc.lipid17`, AmberTools **24.8**, generated **2025-04-30**.
This is the conversion source version, not a claim that every Amber component
currently installed in the container is version 24.8.

The XML embeds the same six references as the
[0.16.0 conversion recipe](https://github.com/openmm/openmmforcefields/blob/0.16.0/amber/biopolymer.yaml):

| BibTeX key | DOI | Role |
| --- | --- | --- |
| `Skjevik2012Lipid11` | `10.1021/jp3059992` | Modular lipid framework |
| `Dickson2012GAFFlipid` | `10.1039/C2SM26007G` | Lipid parameter development |
| `Dickson2014Lipid14` | `10.1021/ct4010307` | Lipid14 foundation |
| `Skjevik2015LipidAssembly` | `10.1039/c4cc09584g` | Family validation / self-assembly study |
| `Skjevik2016LipidAssembly` | `10.1039/c5cp07379k` | Family validation / self-assembly study |
| `Madej2015Cholesterol` | `10.1021/acs.jpcb.5b04924` | Cholesterol parameterization |

Resolution: retain all six as **distribution-declared references** associated with
Lipid17 and distinguish that reason from actual operations. This is a conservative
complete export of the shipped citation block, not proof that all six methods or
lipid species were used. In particular, citing a self-assembly validation study
must not cause Methods to say that a PACKMOL-built bilayer self-assembled; a
cholesterol reference does not establish that the system contained cholesterol.
The structured report should separately identify actual lipid composition.

Do not invent a DOI for the frequently mentioned unpublished Lipid17 manuscript,
or relabel Lipid17 as Lipid14/Lipid21. The official
[Amber lipid page](https://ambermd.org/AmberModels_lipids.php) distinguishes
Lipid21 as an extension of Lipid14/Lipid17 and links the Lipid14 foundation paper.
The released parameter files plus their declared papers are a usable, traceable
citation route without a standalone Lipid17 publication.

The primary [GAFFlipid publisher page](https://pubs.rsc.org/en/content/articlehtml/2012/sm/c2sm26007g)
gives pages **9617–9627**; Crossref only supplied the first page. The BibTeX uses
the full publisher range. The 2016 paper title is **Simulation** (singular), not
the “Simulations” typo in the conversion recipe.

## phosfb18: original paper plus correction, with parameter checks

Use `Stoppelman2021FB18`:
[Development and Validation of AMBER-FB15-Compatible Force Field Parameters for Phosphorylated Amino Acids](https://doi.org/10.1021/acs.jpcb.1c07547),
J. Phys. Chem. B **125**, 11927–11942 (2021), together with
`Stoppelman2022FB18Correction`, the
[2022 correction](https://doi.org/10.1021/acs.jpcb.2c06820), **126**, 8596.

The correction concerns an incorrectly ordered torsion-term file in the original
supplement, which was not the file used for the paper's simulations. The authors
provided a corrected file and a repository. Two traps were checked:

- The [author repository README](https://github.com/pnerenberg/amber-fb18/tree/77664f0fd8db10b18400d7c5407e149c1a6409ca)
  points to `10.1021/acs.jpcb.1c10971` for the original paper. That DOI is a
  [different DNA-parameter paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC9234960/).
  The correction's bibliography and the repository's own
  [leaprc header](https://github.com/pnerenberg/amber-fb18/blob/77664f0fd8db10b18400d7c5407e149c1a6409ca/leaprc.phosfb18)
  identify the correct DOI, `1c07547`.
- A matching title or a post-2022 package date alone does not prove the corrected
  parameters were incorporated. Actual files were checked below.

The container's `/opt/mdclaw/dat/leap/parm/frcmod.phosfb18` is **byte-identical**
to the [author's corrected file at a fixed commit](https://github.com/pnerenberg/amber-fb18/blob/77664f0fd8db10b18400d7c5407e149c1a6409ca/frcmod.phosfb18).
Both SHA-256 values are
`f7da61f4fcd3dd4f9a602205fe9271d6560b195aec663a54f443c815442448cf`.

The converted `amber/phosfb18.xml` was checked independently: all **55 proper
torsion groups / 235 Fourier terms** match the corrected frcmod after converting
kcal/mol to kJ/mol and degrees to radians. Group keys, term count, periodicity,
phase and amplitude all agree; maximum absolute numeric difference was **0**.
The comparison preserved atom-type case, wildcard identity, term order and signed
amplitudes. Thus the specific corrected torsion data are present in this XML.
This check is not an all-interaction/trajectory equivalence test and must not be
generalized to other containers or parameter files.

The author's leaprc explicitly pairs FB18 with **AMBER-FB15**, not ff14SB/ff19SB.
Include the existing `Wang2017FB15` paper when that base is actually loaded.
The paper's `fb3mod` water recipe uses TIP3P-FB with Joung–Cheatham SPC/E ion
parameters; do not infer this exact water/ion recipe solely from `phosfb18`.
MDClaw's `fb15` catalog entry selects `phosfb18`, but a report must record the
actual resolved water and ion files separately.

## phosaa10: separate charge and phosphate-oxygen parameter sources

The container's `dat/leap/cmd/leaprc.phosaa10` explicitly attributes charges to
existing `Homeyer2006PhosphoAA` and phosphate-oxygen van der Waals parameters to
new `Steinbrecher2012Phosphates`:
[Revised AMBER Parameters for Bioorganic Phosphates](https://doi.org/10.1021/ct300613v),
J. Chem. Theory Comput. **8**, 4405–4412 (2012).
The same pair is embedded in `amber/phosaa10.xml` and the conversion recipe.

Use both for this shipped phosaa10 set. Do not extrapolate from the Homeyer paper's
title to say every described phosphohistidine is present: the inspected leaprc
lists SEP/TPO/PTR and singly charged S1P/T1P/Y1P. Its intended protein family is
ff99SB and related older variants, not a substitute for phosaa14SB/phosaa19SB.

## Inspected artifacts and reproducibility boundary

Paths below are inside the SIF, relative to `/opt/mdclaw/` unless stated otherwise.
The XML prefix is `lib/python3.12/site-packages/openmmforcefields/ffxml/amber/`.

| Artifact | SHA-256 |
| --- | --- |
| `lipid17.xml` under XML prefix | `cea15a74f1d819667ccce81c8a3cf3c44700b8f1d673c8458ae16e3335448f34` |
| `phosfb18.xml` under XML prefix | `4ded17997a7fb0150a603f6b242beedb652822f71aead416e5992713c636868a` |
| `phosaa10.xml` under XML prefix | `27d2aecc0d35d5febfc14cdec38bb691e81fcb442bd75997ae41a389544715e4` |
| `dat/leap/cmd/oldff/leaprc.lipid17` | `f8cda73ca206bc31f14af49006e2c2d6c3d30a853637a2e20218a2f84a9164d3` |
| `dat/leap/parm/lipid17.dat` | `8165899354c80bbbbae4a7d7413726135b0a05287e754237e40b56fd13bb6573` |
| `dat/leap/lib/oldff/lipid17.lib` | `1aa1d697ebca330e7953ebcc200900e9ba09d39d21fd53e162d7086ecf3e6e14` |

The inspected SIF is `/home/yasu/tmp/mdclaw/mdclaw/mdclaw.sif` (5,656,682,496
bytes at inspection); identity of the relevant contents is pinned by the hashes
above, not inferred from the mutable SIF filename. XML source-package fields are
conversion provenance, and their embedded source MD5 alone does not hash every
transitive parameter file. No simulations, runtime code, or skill text changed.

Other previously listed issues such as arbitrary user XML, NAGL model identity,
ion combinations and platform-specific algorithms remain distinct from these
three resolved bibliography gaps. They require their own actual inputs; resolving
these citations does not retroactively verify all historical jobs.
