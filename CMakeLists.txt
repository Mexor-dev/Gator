cmake_minimum_required(VERSION 3.20)
project(gator_native_kernel LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(GATOR_ENABLE_CUDA "Enable CUDA backend if available" ON)
option(GATOR_ENABLE_ROCM "Enable ROCm/HIP backend if available" ON)
option(GATOR_ENABLE_ONEAPI "Enable oneAPI backend if available" ON)

set(GATOR_BACKEND "cpu")

if(GATOR_ENABLE_CUDA)
  include(CheckLanguage)
  check_language(CUDA)
  if(CMAKE_CUDA_COMPILER)
    enable_language(CUDA)
    set(GATOR_BACKEND "cuda")
    add_compile_definitions(GATOR_BACKEND_CUDA=1)
  endif()
endif()

if(GATOR_BACKEND STREQUAL "cpu" AND GATOR_ENABLE_ROCM)
  find_package(HIP QUIET)
  if(HIP_FOUND)
    set(GATOR_BACKEND "rocm")
    add_compile_definitions(GATOR_BACKEND_ROCM=1)
  endif()
endif()

if(GATOR_BACKEND STREQUAL "cpu" AND GATOR_ENABLE_ONEAPI)
  find_package(IntelDPCPP QUIET)
  if(IntelDPCPP_FOUND)
    set(GATOR_BACKEND "oneapi")
    add_compile_definitions(GATOR_BACKEND_ONEAPI=1)
  endif()
endif()

add_subdirectory(src/inference)

message(STATUS "Gator backend selected: ${GATOR_BACKEND}")
