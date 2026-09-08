# Investigating residue, bond or charge discrepancies

Use this page when counts disagree, a protonation/disulfide state appears lost,
or membrane neutralization fails.

1. Record the actual runtime: CLI version, resolved SIF, source import path and
   any checkout overlay, plus the installed skill location. A current checkout
   does not prove that a container imported it.
2. Run `inspect_job` and select the exact input/output nodes. On a failed node,
   run `trace_failure`. Compare declared conditions, node metadata, append-only
   events and failure artifacts; if they disagree, report the discrepancy.
   Do not edit terminal node statuses or reconstruct missing historical steps
   from a later successful branch.
3. Compare residue/atom identities in the input and output artifacts, including
   chain, residue number and insertion code. Distinguish all-residue count,
   amino-acid sequence length, residue-name filters and a viewer's `protein`
   selection. AMBER variants remain protein; ACE/NME are caps and contribute
   no amino-acid sequence positions. A selection/count discrepancy alone does
   not establish physical deletion or a backbone break.
4. Read `topology_validation`: `input_conservation` compares heavy-atom identity
   across loading; `core` compares output Topology/System/State; `loader`
   identifies Pablo or validated PDBFile fallback. A Pablo warning is not proof
   of residue loss. Verify requested S–S atom pairs in the System and topology,
   and compare force-field charge with the recorded neutralization intent.
5. `disulfide_chemistry_conflict` identifies an incompatible prepared state or
   ambiguous endpoint. Resolve the chemical state on a new prep branch and
   propagate its plan through solv and topo. Do not merely rename CYS to CYX,
   remove HG in topo, widen a distance cutoff, patch charges, or substitute
   another branch's bond plan to make the build pass. Free/metal-bound CYM and
   reduced CYS can be valid states.
6. For patch-tile membranes, examine
   `statistics.neutralization.charge_evaluation`, the raw force-field charge,
   resolved pair checks and ion counts. `membrane_neutralization_failed` can
   wrap the more specific `charge_evaluation_code`. A missing/nonfinite or
   nonintegral charge (`net_charge_invalid`) is not zero. Rebuild on a new solv
   branch after correcting the prep input; keep the final topology's
   `neutralization_charge_mismatch` check.

State what is observed, what is reproduced, and what history is unavailable.
Keep alternative physical states separate; reduced and oxidized preparations
are not interchangeable controls for a claim of residue loss.
