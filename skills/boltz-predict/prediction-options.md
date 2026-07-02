# Prediction Options

Three options tune a Boltz-2 run. In `autonomous` mode, use the default column
without asking; in `human_in_the_loop` mode, offer the alternatives.

| Option | Flag | Autonomous default | Alternative |
|---|---|---|---|
| MSA | `--msa-path <file>` | Boltz-2 MSA server (omit the flag) | Custom MSA file for tailored alignments |
| Affinity (protein-ligand only) | `--affinity` / `--no-affinity` | `--no-affinity` (structure-only, faster) | `--affinity` when binding affinity is wanted |
| Number of models | `--num-models N` | `1` (fastest) | `3-5` for an ensemble to rank or pick conformers |

Custom MSA caveat: the `mdclaw` wrapper accepts a single `--msa-path` value and
is best for single-protein inputs. For multi-protein custom MSA workflows, fall
back to the MSA server unless the user explicitly wants to hand-prepare Boltz
YAML (a multimer + custom MSA request returns
`boltz_custom_msa_multimer_unsupported`).
