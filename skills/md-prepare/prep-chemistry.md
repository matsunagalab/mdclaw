# Prep Chemistry Edge Cases

Conditional chemistry handling during `prepare_complex`. Read only the section
that applies. None of this is part of the normal happy path.

## Site-specific protonation

If the user names specific residue protonation states, pass them explicitly
through `protonation_states`; do not leave them as a free-text note, e.g.
`{"A:57": "HIP", "A:25": "ASH"}` or a list of `{chain, resnum, state}` records.
Supported Amber variants: ASP/ASH, GLU/GLH, HID/HIE/HIP, LYS/LYN, CYS/CYX/CYM.
General pH-aware protonation is otherwise handled by `clean_protein`.

## Terminal caps

If the user requests terminal caps, use `--n-terminal-cap ACE` and/or
`--c-terminal-cap NME`; `--cap-termini` is only the shorthand for both.
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
