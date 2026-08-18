"""Membrane topology server package.

Predicts which residues cross the bilayer and which side the non-membrane
stretches sit on, so that membrane embedding does not have to infer the
topology from the 3D structure alone.
"""

from mdclaw.membrane_topology.tmbed import predict_membrane_topology

TOOLS = {
    fn.__name__: fn
    for fn in (
        predict_membrane_topology,
    )
}

__all__ = [*TOOLS, "TOOLS"]
