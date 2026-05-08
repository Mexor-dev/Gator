add_library(gator_kern SHARED gator_kern.cpp)

target_include_directories(gator_kern
  PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)

target_compile_features(gator_kern PRIVATE cxx_std_17)
