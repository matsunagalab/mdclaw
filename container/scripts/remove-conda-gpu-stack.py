"""Remove the lock's GPU binaries before installing the container's CUDA 11.8 stack.

Build-only: execute against the disposable stage-1 prefix, never a user's Pixi
environment. Remove package-owned files and metadata together; a pip uninstall
alone leaves Conda's libtorch/OpenMM/CUDA libraries behind.
"""

import json
from pathlib import Path
import re
import sys


GPU_PREFIXES = ("cuda-", "libcublas", "libcud", "libcufft", "libcufile",
                "libcurand", "libcusolver", "libcusparse", "libnv")
REPLACED_APIS = {"openmm", "pytorch", "jaxlib", "triton"}


def removal_names(records):
    names = {name for name in records if name.startswith(GPU_PREFIXES)}
    names |= {"cudnn", "nccl", "libmagma", "libtorch"} & records.keys()
    # Follow dependencies through GPU libraries, stopping at APIs replaced by
    # source/wheels. Keep their callers (pdbfixer, pymbar, NAGL, etc.).
    while True:
        added = {name for name, record in records.items() if name not in names
                 and any(re.split(r"[ <>=!~]", dep)[0] in (names - REPLACED_APIS) | {"__cuda"}
                         for dep in record.get("depends", []))}
        if not added:
            break
        names |= added
    return names | (REPLACED_APIS & records.keys())


def remove_stack(prefix):
    records = {}
    metadata = {}
    for path in (prefix / "conda-meta").glob("*.json"):
        record = json.loads(path.read_text())
        records[record["name"]] = record
        metadata[record["name"]] = path
    if not {"openmm", "pytorch", "libtorch", "jaxlib"} <= records.keys():
        raise RuntimeError("Expected the complete locked Linux GPU stack before replacement")
    names = removal_names(records)
    # Abort if a new dependency would remove scientific tools too. This list is
    # a boundary check, not the deletion list (which follows the actual graph).
    unexpected = names - {n for n in names if n.startswith(GPU_PREFIXES)}
    unexpected -= REPLACED_APIS | {"cudnn", "nccl", "libmagma", "libtorch"}
    if unexpected:
        raise RuntimeError(f"Unexpected GPU dependents; review the lock: {sorted(unexpected)}")
    keep = {file for name, record in records.items() if name not in names
            for file in record.get("files", [])}
    remove = {file for name in names for file in records[name].get("files", [])} - keep
    # Validate the entire plan before deleting anything. Shared paths such as
    # LICENSE stay owned by the retained packages.
    for file in remove:
        relative = Path(file)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid package path: {file}")
        if not (prefix / relative).parent.resolve().is_relative_to(prefix.resolve()):
            raise ValueError(f"Package path escapes prefix: {file}")
    for file in sorted(remove):
        path = prefix / file
        if path.is_symlink() or path.is_file():
            path.unlink()
    for name in names:
        metadata[name].unlink()
    print("Removed Conda GPU stack:", ", ".join(sorted(names)))


if __name__ == "__main__":
    prefix = Path(sys.prefix)
    if prefix != Path("/build/.pixi/envs/default"):
        raise RuntimeError("Only run this helper in the disposable Docker build prefix")
    remove_stack(prefix)
