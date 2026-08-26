# Prep Chemistry

Chemistry decisions taken during `prepare_complex`. Read the sections that
apply. Every protein preparation needs a protonation baseline; the rest are
conditional.

## Site-specific protonation

If the user names specific residue protonation states, pass them explicitly
through `protonation_states`; do not leave them as a free-text note, e.g.
`{"A:57": "HIP", "A:25": "ASH"}` or a list of `{chain, resnum, state}` records.
Supported Amber variants: ASP/ASH, GLU/GLH, HID/HIE/HIP, LYS/LYN, CYS/CYX/CYM.
The selected standard or pH-aware baseline is otherwise handled by
`clean_protein`; explicit site states are overlaid after it.

This is for a handful of named sites. If what the user wants is every side
chain in its standard state, do not enumerate them here - see "Standard states
versus predicted ones" below.

## Disulfide bonds

`prepare_complex` detects disulfides from the geometry and forms them, which is
what a deposit that declares them wants. Two things are worth knowing because
neither is visible in the output until it is too late to change cheaply.

The detection is on by default. A pair of cysteines within bonding distance
becomes a bond and the residues are renamed CYX, whether or not anyone asked.
The set that was formed is reported after the fact, in the prep node's
`disulfide_pairs`; there is no preflight that tells you first, so when the
cysteine chemistry matters, decide before running rather than after.

Suppression exists and is explicit:

| what you want | how |
|---|---|
| no disulfides at all | `--disulfide-pairs '[]'` |
| all but one | list the pairs to keep, or give the unwanted one `"form_bond": false` |
| the detected set | pass nothing |

The empty list is honoured rather than ignored: preparation gates on the
argument being present, not on it being non-empty, so `'[]'` means none rather
than falling back to detection.

Reach for suppression when the request asks for reduced cysteines, or names a
reference state that has no disulfide. Do not reach for it to make a downstream
step succeed - a disulfide that the deposit's geometry supports is part of the
system, and removing it changes what is being simulated. "Neutral cysteine" on
its own is not an instruction to break a bond: a disulfide-bonded CYX is
neutral, and so is a free CYS.

### A disulfide inside a rebuilt gap

Detection reads the structure as deposited, so a disulfide whose cysteines are
*unresolved* is not found: there is no SSBOND record for it and no distance to
measure. If those residues are later rebuilt, the bond does not appear on its
own, and the two systems you are comparing can end up differing by a disulfide
that has nothing to do with the variable under study.

Measured on TAS1R2-TAS1R3: 9UTC resolves CYS363-CYS366 on chain A at 2.04 A,
while 9UT9 leaves 341-367 unresolved. Detection found 17 bonds in one and 16 in
the other, and the apo cysteines came out of the rebuild 11.65 A apart.

When one structure in a comparison resolves a disulfide the other does not,
pass the full set explicitly to **both** with `--disulfide-pairs`, taking the
better-resolved deposit as the reference. MODELLER is told about the bonds and
builds the loop with them restrained, so the geometry is right at the point the
bond is formed. Declaring the bond without that only hands minimisation a bond
stretched several angstroms past equilibrium.

## Standard states versus predicted ones

`--ph` alone runs propka, which predicts each titratable side chain's charge
state from its local environment. At pH 7 that returns neutral ASH, GLH or LYN
for some residues, which is a prediction, not a default.

When the request asks for standard states - "charged aspartate, glutamate,
lysine and arginine, neutral histidine and cysteine" is the usual phrasing -
pass `--protonation-method standard`. It skips the prediction and keeps the
force field's own states. Do not try to reach the same place by listing every
residue propka moved in `--protonation-states`: that is one override per
residue, each addressed by a chain ID whose space is easy to get wrong, and
each a chance to fail the whole prep.

Input protonation names are a separate choice. By default they do not override
the selected baseline, so `standard` really means all-standard. Add
`--preserve-input-protonation` only when deposited ASH/GLH/LYN or histidine
variants are part of the requested chemical state. This switch never controls
CYX disulfides or metal-site CYM: those come from the disulfide and metal
coordination contracts and remain structural chemistry in either mode.

`--protonation-states` still wins where it is given, so the two combine: ask
for standard states, then name the handful of sites that genuinely differ.

## Terminal caps

If the user requests terminal caps, use `--n-terminal-cap ACE` and/or
`--c-terminal-cap NME`; `--cap-termini` is only the shorthand for both. Each
side is independent - a C-terminal cap alone is fine.

A deposit can arrive already capped. ACE and NME count as protein, so a cap
that came in with the structure is kept and counted as a residue, which changes
the residue count the system is compared against. `prepare_complex` reports the
caps it found on the input, and warns when it keeps them. Pass
`--strip-input-caps` to simulate the chain uncapped. Keep them when the deposit
means them, which is the usual case; strip them when the target system is the
free terminus.
Cap-residue hydrogen completion is tool-owned in `prepare_complex`. When the
user specifies a non-default protein force field for the eventual topology, pass
the same value as `--terminal-cap-forcefield`; otherwise the ff19SB default
applies.

## DNA / RNA hydrogen rebuild

For standard DNA/RNA, `prepare_complex` treats them as nucleic polymers (not
ligands) and rebuilds nucleic hydrogens with OpenMM Modeller using the current
DNA.OL15 / RNA.OL3 libraries before topology. No extra flags are needed.

## Isotopes (deuterium) and isotope-preserving MD

Experimental isotope atoms such as deuterium are excluded by `prepare_complex`
across split components from the default classical MD path, then standard
hydrogens are rebuilt. Copy the tool-written `component_disposition.json` rather
than hand-writing it. If the user explicitly asks for isotope-preserving MD,
treat it as unsupported for now and stop with a structured explanation instead
of silently converting D to H.

## Glycoproteins

Prep preserves glycan provenance and linkages. Amber/GLYCAM conversion,
bond-plan application, and glycan-only hydrogen completion are topology
normalization artifacts written by `build_amber_system`, not by prep.

## Large assemblies and chain identity

For biological assemblies or systems with many chains, do not treat the
one-character PDB chain ID in `merged_pdb` as canonical identity. Read
`chain_identity_map.json` and use `component_id`, source label/auth IDs,
topology chain index, and atom/residue ranges to identify components. To request
a biological assembly, use `fetch_structure --assembly-ids <id...>` or
`--assembly-mode preferred|all`, then select the intended source candidate
during `prepare_complex` (see `skills/md-prepare/acquisition.md`).
