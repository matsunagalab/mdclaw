"""node package.

Behavior-preserving split of the former ``mdclaw._node`` module.
Importers continue to use the ``mdclaw._node`` shim, which re-exports
every submodule name; this package exposes the submodules only."""
