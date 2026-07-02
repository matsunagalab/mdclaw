# MDClaw Defaults

The solvent-regime mapping, the explicit-water constant defaults
(`ff19SB + opc`, 15 Å buffer, 0.15 M NaCl, 300 K / 1 bar, HMR 4 fs, HBonds,
PME), and the associated guardrails are consolidated in
`skills/common/solvent-regimes.md`. Read that page; it is the single source of
truth for defaults.

Do not substitute legacy tutorial defaults such as `ff14SB + tip3p` unless the
user explicitly requests them and guardrails allow the combination.
