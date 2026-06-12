"""Node-based job graph management (schema v3).

Each pipeline step (prep, solv, topo, min, eq, prod) is a *node* with its own
directory, ``node.json``, lock file, and ``artifacts/`` folder.  Parent-child
relationships form a DAG.  ``progress.json`` is a thin index of nodes.

Design principle:
    skill = what to run (orchestration, no state mutation)
    tool  = run + record (execution + state via this module)
"""

import logging


logger = logging.getLogger(__name__)


NODE_TYPES = frozenset({
    "source", "prep", "solv", "topo", "min", "eq", "prod", "analyze",
})


NODE_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed"})


NODE_STATUS_ALIASES = {
    "submitted": "queued",
}


ANALYSIS_DATA_SCOPES = frozenset({"segment", "production_chain", "comparison"})


COMPARISON_MAPPING_TYPES = frozenset({"residue_number", "atom_selection"})


IMMUTABLE_NODE_UPDATE_KEYS = frozenset({
    "schema_version",
    "node_id",
    "node_type",
    "type",
    "parent_node_ids",
    "parents",
    "dependency_node_ids",
    "dependencies",
    "conditions",
    "created_at",
})


OPERATIONAL_METADATA_KEYS = ("claimed_by", "claim_expires_at", "open_needs")


SCHEMA_VERSION = 3


_STRUCTURED_ARTIFACT_PATH_KEYS = frozenset({
    "path",
    "raw_file",
    "mol2",
    "mol2_file",
    "sdf",
    "sdf_file",
    "coordinate_file",
    "frcmod",
    "frcmod_file",
    "frcmods",
    "pdb",
    "pdb_file",
    "combined_trajectory",
    "combined_energy",
    "fitted_trajectory",
    "trajectory",
    "trajectory_file",
    "energy",
    "energy_file",
    "reference_pdb",
    "selection_indices",
    "overlay_plot",
    "source_trajectories",
    "source_energy_files",
    "rmsd_timeseries",
    "rmsd_csv",
    "rmsd_plot",
    "distance_timeseries",
    "distance_csv",
    "distance_plot",
    "q_timeseries",
    "q_csv",
    "q_plot",
    "rmsf_values",
    "rmsf_csv",
    "rmsf_plot",
    "rmsf_metadata",
    "contact_frequency_matrix",
    "contact_frequency_csv",
    "contact_frequency_plot",
    "contact_pairs_metadata",
    "result_json",
    "analysis_manifest",
    "analysis_script",
    "notebook",
    "csv",
    "plot",
    "figure",
    "table",
    "timeseries",
    "report",
    "model",
    "clusters",
    "projection",
    "source_bundle",
    "source_selection",
})


_ALLOWED_PARENT_TYPES = {
    "source": frozenset(),
    # prep can consume a source artifact or transform an existing prep node
    # (mutation/re-preparation branches).
    "prep": frozenset({"source", "prep"}),
    "solv": frozenset({"prep"}),
    # explicit-water topo descends from solv; implicit topo skips solv and
    # descends directly from prep.
    "topo": frozenset({"solv", "prep"}),
    # min owns force-field-level coordinate relaxation after topology
    # generation. It writes a portable state artifact that eq can resume
    # from without embedding minimization work in the eq node.
    "min": frozenset({"topo", "min"}),
    # New equilibration nodes should parent from min. topo remains accepted
    # as a compatibility fallback for older DAGs; eq → eq chaining lets users
    # compose multi-stage equilibration (e.g. NPT → NVT → NPT) with one
    # ensemble per node and per-stage restraint settings.
    "eq": frozenset({"min", "topo", "eq"}),
    "prod": frozenset({"eq", "prod"}),
    "analyze": frozenset({"prod", "analyze"}),
}


# Preferred forward parent type(s) used when ``create_node`` is called
# without explicit ``parent_node_ids``. Only the canonical forward edge is
# auto-resolved; same-type chaining (prep->prep, eq->eq, prod->prod, ...) and
# multi-parent analyze comparisons are deliberately excluded so that branch
# and extension intent always stays explicit. ``source`` has no parent.
_AUTO_PARENT_PREFERENCE = {
    "prep": ("source",),
    "solv": ("prep",),
    "topo": ("solv", "prep"),
    "min": ("topo",),
    "eq": ("min", "topo"),
    "prod": ("eq",),
    "analyze": ("prod",),
}


_LABEL_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)


_RESTART_NODE_ID_UNSET = object()
