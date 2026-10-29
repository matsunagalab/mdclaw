#!/bin/bash
set -ex
# The CUDA 11.8 toolkit lives in the build prefix next to nvcc.
CMAKE_FLAGS="${CMAKE_ARGS} -DCMAKE_INSTALL_PREFIX=${PREFIX} -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"
CMAKE_FLAGS+=" -DCUDAToolkit_ROOT=${BUILD_PREFIX} -DCUDA_TOOLKIT_ROOT_DIR=${BUILD_PREFIX}"
CMAKE_FLAGS+=" -DCMAKE_CUDA_COMPILER=${BUILD_PREFIX}/bin/nvcc -DCMAKE_CUDA_HOST_COMPILER=${CXX}"
CMAKE_FLAGS+=" -DOPENMM_BUILD_CUDA_LIB=ON -DOPENMM_BUILD_CUDA_TESTS=OFF"
CMAKE_FLAGS+=" -DOPENMM_BUILD_OPENCL_LIB=ON -DOPENMM_BUILD_HIP_LIB=OFF"
CMAKE_FLAGS+=" -DOPENMM_BUILD_C_AND_FORTRAN_WRAPPERS=OFF"
# The 11.8 toolkit sits in the build prefix; conda's CMAKE_ARGS search only the host prefix.
CMAKE_FLAGS+=" -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH"
CMAKE_FLAGS+=" -DOPENCL_INCLUDE_DIR=${PREFIX}/include -DOPENCL_LIBRARY=${PREFIX}/lib/libOpenCL${SHLIB_EXT}"
CMAKE_FLAGS+=" -DSWIG_EXECUTABLE=$(which swig) -DPYTHON_EXECUTABLE=${PYTHON}"
CFLAGS+=" -O3 -ldl"
CXXFLAGS+=" -O3 -ldl"
mkdir -p build && cd build
cmake ${CMAKE_FLAGS} ${SRC_DIR}
make -j${CPU_COUNT}
make -j${CPU_COUNT} install PythonInstall
mkdir -p ${PREFIX}/share/openmm/
mv ${PREFIX}/examples ${PREFIX}/share/openmm/ || true
for lib in ${PREFIX}/lib/plugins/*${SHLIB_EXT}; do
    ln -s $lib ${PREFIX}/lib/$(basename $lib) || true
done
