#!/usr/bin/env bash
# Same source build for a conda prefix or the container's source-built OpenMM.
# Usage: bash build-plumed.sh PREFIX NEW_BUILD_DIRECTORY [JOBS]
set -euo pipefail
prefix=$(cd "$1" && pwd)
build_dir=$2
jobs=${3:-4}
test -x "$prefix/bin/python"
test -f "$prefix/include/OpenMM.h"
test ! -e "$build_dir"  # never reuse or erase someone else's build tree
mkdir -p "$build_dir"
build_dir=$(cd "$build_dir" && pwd)
plumed_commit=52da2bb76d37dbad0d19f59c6fe0b2ab939e3ded # v2.9.4
plugin_commit=95bfd46d6499625de03ea2151aec42edeae5f662 # v2.1
fetch() {
    git init -q "$2"
    git -C "$2" remote add origin "$1"
    git -C "$2" fetch --depth 1 origin "$3"
    git -C "$2" checkout -q FETCH_HEAD
}
fetch https://github.com/plumed/plumed2.git "$build_dir/plumed" "$plumed_commit"
cd "$build_dir/plumed"
./configure --prefix="$prefix" --disable-mpi --disable-python --disable-libtorch
make -j "$jobs"
make install
fetch https://github.com/openmm/openmm-plumed.git "$build_dir/plugin" "$plugin_commit"
# setBox retains a pointer until performCalcNoUpdate. Upstream 2.1's block-local
# array dies too early and optimized builds can silently lose periodicity.
patch -d "$build_dir/plugin" -p1 <<'PATCH'
--- a/openmmapi/src/PlumedForceImpl.cpp
+++ b/openmmapi/src/PlumedForceImpl.cpp
@@ -132,2 +132,2 @@
-    if (usesPeriodic) {
-        Vec3 boxVectors[3];
+    Vec3 boxVectors[3];
+    if (usesPeriodic) {
PATCH
cmake -S "$build_dir/plugin" -B "$build_dir/plugin/build" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
    -DOPENMM_DIR="$prefix" -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DPLUMED_INCLUDE_DIR="$prefix/include/plumed" -DPLUMED_LIBRARY_DIR="$prefix/lib" \
    -DCMAKE_INSTALL_RPATH="$prefix/lib" -DPYTHON_EXECUTABLE="$prefix/bin/python" \
    -DPIP_EXECUTABLE="$prefix/bin/pip"
cmake --build "$build_dir/plugin/build" --parallel "$jobs"
cmake --install "$build_dir/plugin/build"
# Upstream PythonInstall uses isolated pip builds without declaring NumPy as a
# build dependency. Generate its wrapper, then build against this exact prefix.
cd "$build_dir/plugin/build/python"
swig -python -c++ -o PlumedPluginWrapper.cpp "-I$prefix/include" "$build_dir/plugin/python/plumedplugin.i"
LDFLAGS="-Wl,-rpath,$prefix/lib ${LDFLAGS:-}" "$prefix/bin/python" -m pip install --no-build-isolation --no-deps .
"$prefix/bin/python" -c 'import openmm, openmmplumed; f=openmmplumed.PlumedForce(""); assert hasattr(f,"setMasses") and hasattr(f,"setRestart"); print("OpenMM", openmm.__version__, "PLUMED plugin ready")'
"$prefix/bin/python" -c 'import json, pathlib, sys, openmm; p=pathlib.Path(sys.prefix)/"share/mdclaw"; p.mkdir(parents=True, exist_ok=True); (p/"plumed-build.json").write_text(json.dumps(dict(plumed="2.9.4", plugin="2.1", plugin_patch="box-pointer-lifetime-v1", plumed_commit=sys.argv[1], plugin_commit=sys.argv[2], openmm=openmm.__version__), indent=2)+"\n")' "$plumed_commit" "$plugin_commit"
