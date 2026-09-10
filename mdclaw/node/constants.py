"""Schema-v3 node constants: types, statuses, parent-type table."""

import logging
import re


logger = logging.getLogger(__name__)


NODE_TYPES = frozenset({
    "source", "prep", "solv", "topo", "min", "eq", "prod", "analyze",
})


NODE_TYPE_ORDER = ("source", "prep", "solv", "topo", "min", "eq", "prod", "analyze")

# Long names agents type for a stage; accepted silently by create_node.
NODE_TYPE_ALIASES = {
    "minimization": "min", "minimisation": "min", "minimize": "min", "minimise": "min",
    "equilibration": "eq", "equilibrate": "eq", "equil": "eq",
    "production": "prod", "prod_md": "prod",
    "topology": "topo", "top": "topo",
    "solvation": "solv", "solvate": "solv", "solvent": "solv",
    "preparation": "prep", "prepare": "prep",
    "analysis": "analyze", "analyse": "analyze",
}

# Words agents invent from tool names, mapped to the stage that does that work
# (observed 2026-09-10: 'split', 'membrane', 'fetch', 'build').
NODE_TYPE_SUGGESTIONS = {
    "fetch": "source", "register": "source", "structure": "source", "pdb": "source",
    "download": "source", "acquire": "source",
    "split": "prep", "clean": "prep", "merge": "prep", "complex": "prep", "protein": "prep",
    "ligand": "prep", "mutate": "prep", "mutation": "prep",
    "membrane": "solv", "embed": "solv", "water": "solv", "box": "solv", "ions": "solv",
    "build": "topo", "amber": "topo", "openmm": "topo", "system": "topo", "forcefield": "topo",
    "md": "prod", "run": "prod", "simulation": "prod", "simulate": "prod", "dynamics": "prod",
    "trajectory": "analyze", "rmsd": "analyze", "rmsf": "analyze", "analyze_rmsd": "analyze",
}


def normalize_node_type(value):
    """Canonical node type for ``value`` (an alias or the type itself), else None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in NODE_TYPES:
        return text
    return NODE_TYPE_ALIASES.get(text)


def suggest_node_type(value):
    """Best-guess stage for a name that is not a node type, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    for word in (text, *re.split(r"[^a-z]+", text)):
        if word in NODE_TYPES:
            return word
        if word in NODE_TYPE_ALIASES:
            return NODE_TYPE_ALIASES[word]
        if word in NODE_TYPE_SUGGESTIONS:
            return NODE_TYPE_SUGGESTIONS[word]
    return None


NODE_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed"})


TERMINAL_NODE_STATUSES = frozenset({"completed", "failed"})


NODE_STATUS_ALIASES = {
    "submitted": "queued",
}


ANALYSIS_DATA_SCOPES = frozenset({"segment", "production_chain", "comparison"})


COMPARISON_MAPPING_TYPES = frozenset({"residue_number", "atom_selection"})


OPERATIONAL_METADATA_KEYS = ("open_needs",)


SCHEMA_VERSION = 3


DAG_GUIDANCE = (
    "MDClaw CLI manages the job DAG and resolves artifacts between nodes; "
    "do not copy artifacts or pass artifact paths between workflow nodes."
)


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
    "frame_times_ns",
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


CANONICAL_FORWARD_NODE_TYPE = {
    "source": "prep",
    "prep": "solv",
    "solv": "topo",
    "topo": "min",
    "min": "eq",
    "eq": "prod",
    "prod": "analyze",
}


_LABEL_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)


_RESTART_NODE_ID_UNSET = object()
