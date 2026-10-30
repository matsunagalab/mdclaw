#!/bin/bash
set -ex
# Same flags as ../../openmm-torch, but SASS only for compute capability 10.0
# (RIKYU's sm_100) plus PTX for forward compatibility, not every arch of torch.
export TORCH_CUDA_ARCH_LIST="10.0+PTX"
TORCH_DIR=$(${PYTHON} -c "import pathlib, torch; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Torch')")
TORCH_ABI=$(${PYTHON} -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))')
# conda-forge's CUDA headers live under targets/<arch>/include.
export CUDA_INC_PATH=$(ls -d ${BUILD_PREFIX}/targets/*/include)
CMAKE_FLAGS="${CMAKE_ARGS} -DCMAKE_INSTALL_PREFIX=${PREFIX} -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"
CMAKE_FLAGS+=" -DOPENMM_DIR=${PREFIX} -DPYTHON_EXECUTABLE=${PYTHON} -DTorch_DIR=${TORCH_DIR}"
CMAKE_FLAGS+=" -DSWIG_EXECUTABLE=$(which swig)"
CMAKE_FLAGS+=" -DCUDAToolkit_ROOT=${BUILD_PREFIX} -DCMAKE_CUDA_COMPILER=${BUILD_PREFIX}/bin/nvcc"
CMAKE_FLAGS+=" -DCMAKE_CUDA_HOST_COMPILER=${CXX} -DCMAKE_CUDA_ARCHITECTURES=100"
CMAKE_FLAGS+=" -DTORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST}"
CMAKE_FLAGS+=" -DNN_BUILD_CUDA_LIB=ON -DNN_BUILD_OPENCL_LIB=OFF"
CMAKE_FLAGS+=" -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH"
CXXFLAGS+=" -D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI} -DC10_CUDA_NO_CMAKE_CONFIGURE_FILE"
mkdir -p build && cd build
cmake ${CMAKE_FLAGS} ${SRC_DIR}
make -j${CPU_COUNT}
make install
make PythonInstall
