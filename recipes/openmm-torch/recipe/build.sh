#!/bin/bash
set -ex
# Same flags as the container build (container/Dockerfile) and the cuda118
# recipe; CUDA on Linux, OpenCL on macOS (PyTorch is a CPU build there).
TORCH_DIR=$(${PYTHON} -c "import pathlib, torch; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Torch')")
CMAKE_FLAGS="${CMAKE_ARGS} -DCMAKE_INSTALL_PREFIX=${PREFIX} -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"
CMAKE_FLAGS+=" -DOPENMM_DIR=${PREFIX} -DPYTHON_EXECUTABLE=${PYTHON} -DTorch_DIR=${TORCH_DIR}"
CMAKE_FLAGS+=" -DSWIG_EXECUTABLE=$(which swig)"
if [[ "${target_platform}" == linux-* ]]; then
    TORCH_ABI=$(${PYTHON} -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))')
    # sm_86 -> 8.6, sm_100 -> 10.0, sm_120 -> 12.0
    ARCH_LIST=$(${PYTHON} -c "import torch; print(';'.join(f'{a[3:-1]}.{a[-1]}' for a in torch._C._cuda_getArchFlags().split() if a.startswith('sm_')))")
    # conda-forge's CUDA 12 headers live under targets/<arch>/include.
    export CUDA_INC_PATH=$(ls -d ${BUILD_PREFIX}/targets/*/include)
    CMAKE_FLAGS+=" -DCUDAToolkit_ROOT=${BUILD_PREFIX} -DCMAKE_CUDA_COMPILER=${BUILD_PREFIX}/bin/nvcc"
    CMAKE_FLAGS+=" -DCMAKE_CUDA_HOST_COMPILER=${CXX} -DTORCH_CUDA_ARCH_LIST=${ARCH_LIST}"
    CMAKE_FLAGS+=" -DNN_BUILD_CUDA_LIB=ON -DNN_BUILD_OPENCL_LIB=OFF"
    CMAKE_FLAGS+=" -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH"
    CXXFLAGS+=" -D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI} -DC10_CUDA_NO_CMAKE_CONFIGURE_FILE"
else
    CMAKE_FLAGS+=" -DNN_BUILD_CUDA_LIB=OFF -DNN_BUILD_OPENCL_LIB=ON"
    CMAKE_FLAGS+=" -DOPENCL_INCLUDE_DIR=${PREFIX}/include -DOPENCL_LIBRARY=${PREFIX}/lib/libOpenCL${SHLIB_EXT}"
fi
mkdir -p build && cd build
cmake ${CMAKE_FLAGS} ${SRC_DIR}
make -j${CPU_COUNT}
make install
make PythonInstall
