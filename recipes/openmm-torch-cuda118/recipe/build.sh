#!/bin/bash
set -ex
# Same flags as the container build (container/Dockerfile), with the CUDA 11.8
# toolkit in the build prefix and libtorch from the host prefix's PyTorch.
TORCH_DIR=$(${PYTHON} -c "import pathlib, torch; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Torch')")
TORCH_ABI=$(${PYTHON} -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))')
# sm_86 -> 8.6, sm_100 -> 10.0, sm_120 -> 12.0
ARCH_LIST=$(${PYTHON} -c "import torch; print(';'.join(f'{a[3:-1]}.{a[-1]}' for a in torch._C._cuda_getArchFlags().split() if a.startswith('sm_')))")
CMAKE_FLAGS="${CMAKE_ARGS} -DCMAKE_INSTALL_PREFIX=${PREFIX} -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"
CMAKE_FLAGS+=" -DOPENMM_DIR=${PREFIX} -DPYTHON_EXECUTABLE=${PYTHON} -DTorch_DIR=${TORCH_DIR}"
CMAKE_FLAGS+=" -DCUDAToolkit_ROOT=${BUILD_PREFIX} -DCUDA_TOOLKIT_ROOT_DIR=${BUILD_PREFIX}"
CMAKE_FLAGS+=" -DCMAKE_CUDA_COMPILER=${BUILD_PREFIX}/bin/nvcc -DCMAKE_CUDA_HOST_COMPILER=${CXX}"
CMAKE_FLAGS+=" -DTORCH_CUDA_ARCH_LIST=${ARCH_LIST}"
CMAKE_FLAGS+=" -DNN_BUILD_CUDA_LIB=ON -DNN_BUILD_OPENCL_LIB=OFF"
# The 11.8 toolkit sits in the build prefix; conda's CMAKE_ARGS search only the host prefix.
CMAKE_FLAGS+=" -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH"
CMAKE_FLAGS+=" -DSWIG_EXECUTABLE=$(which swig)"
CXXFLAGS+=" -D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI} -DC10_CUDA_NO_CMAKE_CONFIGURE_FILE"
mkdir -p build && cd build
cmake ${CMAKE_FLAGS} ${SRC_DIR}
make -j${CPU_COUNT}
make install
make PythonInstall
