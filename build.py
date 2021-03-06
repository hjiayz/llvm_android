#!/usr/bin/env python
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import datetime
import glob
import logging
import os
import shutil
import subprocess
import utils

import android_version
from version import Version

import mapfile

ORIG_ENV = dict(os.environ)
STAGE2_TARGETS = 'AArch64;ARM;BPF;Mips;X86'


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def check_call(cmd, *args, **kwargs):
    """subprocess.check_call with logging."""
    logger().info('check_call:%s %s',
                  datetime.datetime.now().strftime("%H:%M:%S"),
                  subprocess.list2cmdline(cmd))
    subprocess.check_call(cmd, *args, **kwargs)


def install_file(src, dst):
    """Proxy for shutil.copy2 with logging and dry-run support."""
    import shutil
    logger().info('copy %s %s', src, dst)
    shutil.copy2(src, dst)


def remove(path):
    """Proxy for os.remove with logging."""
    logger().debug('remove %s', path)
    os.remove(path)


def extract_clang_version(clang_install):
    version_file = os.path.join(clang_install, 'include', 'clang', 'Basic',
                                'Version.inc')
    return Version(version_file)


def extract_clang_long_version(clang_install):
    return extract_clang_version(clang_install).long_version()


def pgo_profdata_file(version_str):
    profdata_file = '%s.profdata' % version_str
    profile = utils.android_path('prebuilts', 'clang', 'host', 'linux-x86',
                                 'profiles', profdata_file)
    return profile if os.path.exists(profile) else None


def ndk_base():
    ndk_version = 'r16'
    return utils.android_path('toolchain/prebuilts/ndk', ndk_version)


def android_api(arch, platform=False):
    if platform:
        return '26'
    elif arch in ['arm', 'i386', 'mips']:
        return '14'
    else:
        return '21'


def ndk_path(arch, platform=False):
    platform_level = 'android-' + android_api(arch, platform)
    return os.path.join(ndk_base(), 'platforms', platform_level)


def ndk_libcxx_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++',
                        'include')


def ndk_libcxxabi_headers():
    return os.path.join(ndk_base(), 'sources', 'cxx-stl', 'llvm-libc++abi',
                        'include')


def ndk_toolchain_lib(arch, toolchain_root, host_tag):
    toolchain_lib = os.path.join(ndk_base(), 'toolchains', toolchain_root,
                                 'prebuilt', 'linux-x86_64', host_tag)
    if arch in ['arm', 'i386', 'mips']:
        toolchain_lib = os.path.join(toolchain_lib, 'lib')
    else:
        toolchain_lib = os.path.join(toolchain_lib, 'lib64')
    return toolchain_lib


def support_headers():
    return os.path.join(ndk_base(), 'sources', 'android', 'support', 'include')


# This is the baseline stable version of Clang to start our stage-1 build.
def clang_prebuilt_version():
    return 'clang-4393122'


def clang_prebuilt_base_dir():
    return utils.android_path('prebuilts/clang/host',
                              utils.build_os_type(), clang_prebuilt_version())


def clang_prebuilt_bin_dir():
    return utils.android_path(clang_prebuilt_base_dir(), 'bin')


def clang_prebuilt_lib_dir():
    return utils.android_path(clang_prebuilt_base_dir(), 'lib64')


def arch_from_triple(triple):
    arch = triple.split('-')[0]
    if arch == 'i686':
        arch = 'i386'
    return arch


def clang_resource_dir(version, arch):
    return os.path.join('lib64', 'clang', version, 'lib', 'linux', arch)


def clang_prebuilt_libcxx_headers():
    return utils.android_path(clang_prebuilt_base_dir(), 'include', 'c++', 'v1')


def libcxx_header_dirs(ndk_cxx):
    if ndk_cxx:
        return [
            ndk_libcxx_headers(),
            ndk_libcxxabi_headers(),
            support_headers()
        ]
    else:
        # <prebuilts>/include/c++/v1 includes the cxxabi headers
        return [
            clang_prebuilt_libcxx_headers(),
            utils.android_path('bionic', 'libc', 'include')
        ]


def cmake_prebuilt_bin_dir():
    return utils.android_path('prebuilts/cmake', utils.build_os_type(), 'bin')


def cmake_bin_path():
    return os.path.join(cmake_prebuilt_bin_dir(), 'cmake')


def ninja_bin_path():
    return os.path.join(cmake_prebuilt_bin_dir(), 'ninja')


def check_create_path(path):
    if not os.path.exists(path):
        os.makedirs(path)


def rm_cmake_cache(dir):
    for dirpath, dirs, files in os.walk(dir):
        if 'CMakeCache.txt' in files:
            os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
        if 'CMakeFiles' in dirs:
            utils.rm_tree(os.path.join(dirpath, 'CMakeFiles'))


# Base cmake options such as build type that are common across all invocations
def base_cmake_defines():
    defines = {}

    defines['CMAKE_BUILD_TYPE'] = 'Release'
    defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'
    defines['LLVM_ENABLE_THREADS'] = 'OFF'
    defines['LLVM_LIBDIR_SUFFIX'] = '64'
    defines['LLVM_VERSION_PATCH'] = android_version.patch_level
    defines['CLANG_VERSION_PATCHLEVEL'] = android_version.patch_level
    defines['CLANG_REPOSITORY_STRING'] = 'https://android.googlesource.com/toolchain/clang'
    defines['LLVM_REPOSITORY_STRING'] = 'https://android.googlesource.com/toolchain/llvm'
    return defines


def invoke_cmake(out_path, defines, env, cmake_path, target=None, install=True):
    flags = ['-G', 'Ninja']

    # Specify CMAKE_PREFIX_PATH so 'cmake -G Ninja ...' can find the ninja
    # executable.
    flags += ['-DCMAKE_PREFIX_PATH=' + cmake_prebuilt_bin_dir()]

    for key in defines:
        newdef = '-D' + key + '=' + defines[key]
        flags += [newdef]
    flags += [cmake_path]

    check_create_path(out_path)
    # TODO(srhines): Enable this with a flag, because it forces clean builds
    # due to the updated cmake generated files.
    #rm_cmake_cache(out_path)

    if target:
        ninja_target = [target]
    else:
        ninja_target = []

    check_call([cmake_bin_path()] + flags, cwd=out_path, env=env)
    check_call([ninja_bin_path()] + ninja_target, cwd=out_path, env=env)
    if install:
        check_call([ninja_bin_path(), 'install'], cwd=out_path, env=env)


def cross_compile_configs(stage2_install, platform=False):
    configs = [
        ('arm', 'arm', 'arm/arm-linux-androideabi-4.9/arm-linux-androideabi',
         'arm-linux-android', '-march=armv7-a'),
        ('aarch64', 'arm64',
         'aarch64/aarch64-linux-android-4.9/aarch64-linux-android',
         'aarch64-linux-android', ''),
        ('x86_64', 'x86_64',
         'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'x86_64-linux-android', ''),
        ('i386', 'x86', 'x86/x86_64-linux-android-4.9/x86_64-linux-android',
         'i686-linux-android', '-m32'),
        ('mips', 'mips',
         'mips/mips64el-linux-android-4.9/mips64el-linux-android',
         'mipsel-linux-android', '-m32'),
        ('mips64', 'mips64',
         'mips/mips64el-linux-android-4.9/mips64el-linux-android',
         'mips64el-linux-android', '-m64'),
    ]

    cc = os.path.join(stage2_install, 'bin', 'clang')
    cxx = os.path.join(stage2_install, 'bin', 'clang++')

    for (arch, ndk_arch, toolchain_path, llvm_triple, extra_flags) in configs:
        toolchain_root = utils.android_path('prebuilts/gcc',
                                            utils.build_os_type())
        toolchain_bin = os.path.join(toolchain_root, toolchain_path, 'bin')
        sysroot_libs = os.path.join(ndk_path(arch, platform), 'arch-' + ndk_arch)
        sysroot = os.path.join(ndk_base(), 'sysroot')
        if arch == 'arm':
            sysroot_headers = os.path.join(sysroot, 'usr', 'include',
                                           'arm-linux-androideabi')
        else:
            sysroot_headers = os.path.join(sysroot, 'usr', 'include',
                                           llvm_triple)

        defines = {}
        defines['CMAKE_C_COMPILER'] = cc
        defines['CMAKE_CXX_COMPILER'] = cxx

        # Include the directory with libgcc.a to the linker search path.
        toolchain_builtins = os.path.join(
            toolchain_root, toolchain_path, '..', 'lib', 'gcc',
            os.path.basename(toolchain_path), '4.9.x')
        # The 32-bit libgcc.a is sometimes in a separate subdir
        if arch == 'i386':
            toolchain_builtins = os.path.join(toolchain_builtins, '32')
        elif arch == 'mips':
            toolchain_builtins = os.path.join(toolchain_builtins, '32',
                                              'mips-r2')
        libcxx_libs = os.path.join(ndk_base(), 'sources', 'cxx-stl',
                                   'llvm-libc++', 'libs')
        if ndk_arch == 'arm':
            libcxx_libs = os.path.join(libcxx_libs, 'armeabi')
        elif ndk_arch == 'arm64':
            libcxx_libs = os.path.join(libcxx_libs, 'arm64-v8a')
        else:
            libcxx_libs = os.path.join(libcxx_libs, ndk_arch)

        if ndk_arch == 'arm':
            toolchain_lib = ndk_toolchain_lib(arch, 'arm-linux-androideabi-4.9',
                                              'arm-linux-androideabi')
        elif ndk_arch == 'x86' or ndk_arch == 'x86_64':
            toolchain_lib = ndk_toolchain_lib(arch, ndk_arch + '-4.9',
                                              llvm_triple)
        else:
            toolchain_lib = ndk_toolchain_lib(arch, llvm_triple + '-4.9',
                                              llvm_triple)

        ldflags = [
            '-L' + toolchain_builtins, '-Wl,-z,defs', '-L' + libcxx_libs,
            '-L' + toolchain_lib,
            '--sysroot=%s' % sysroot_libs
        ]
        if arch != 'mips' and arch != 'mips64':
            ldflags += ['-Wl,--hash-style=both']
        defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)
        defines['CMAKE_SYSROOT'] = sysroot
        defines['CMAKE_SYSROOT_COMPILE'] = sysroot

        cflags = [
            '--target=%s' % llvm_triple,
            '-B%s' % toolchain_bin,
            '-isystem %s' % sysroot_headers,
            '-D__ANDROID_API__=%s' % android_api(arch, platform=platform),
            extra_flags,
        ]
        yield (arch, llvm_triple, defines, cflags)


def build_asan_test(stage2_install):
    # We can not build asan_test using current CMake building system. Since
    # those files are not used to build AOSP, we just simply touch them so that
    # we can pass the build checks.
    for arch in ('aarch64', 'arm', 'i686', 'mips', 'mips64'):
        asan_test_path = os.path.join(stage2_install, 'test', arch, 'bin')
        check_create_path(asan_test_path)
        asan_test_bin_path = os.path.join(asan_test_path, 'asan_test')
        open(asan_test_bin_path, 'w+').close()

def build_asan_map_files(stage2_install, clang_version):
    lib_dir = os.path.join(stage2_install,
                           clang_resource_dir(clang_version.long_version(), ''))
    for arch in ('aarch64', 'arm', 'i686', 'x86_64', 'mips', 'mips64'):
        lib_file = os.path.join(lib_dir, 'libclang_rt.asan-{}-android.so'.format(arch))
        map_file = os.path.join(lib_dir, 'libclang_rt.asan-{}-android.map.txt'.format(arch))
        mapfile.create_map_file(lib_file, map_file)

def build_libcxx(stage2_install, clang_version):
    for (arch, llvm_triple, libcxx_defines,
         cflags) in cross_compile_configs(stage2_install):
        logger().info('Building libcxx for %s', arch)
        libcxx_path = utils.out_path('lib', 'libcxx-' + arch)

        libcxx_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libcxx_defines['CMAKE_BUILD_TYPE'] = 'Release'

        libcxx_env = dict(ORIG_ENV)

        libcxx_cmake_path = utils.llvm_path('projects', 'libcxx')
        rm_cmake_cache(libcxx_path)

        invoke_cmake(
            out_path=libcxx_path,
            defines=libcxx_defines,
            env=libcxx_env,
            cmake_path=libcxx_cmake_path,
            install=False)
        # We need to install libcxx manually.
        install_subdir = clang_resource_dir(clang_version.long_version(),
                                            arch_from_triple(llvm_triple))
        libcxx_install = os.path.join(stage2_install, install_subdir)

        libcxx_libs = os.path.join(libcxx_path, 'lib')
        check_create_path(libcxx_install)
        for f in os.listdir(libcxx_libs):
            if f.startswith('libc++'):
                shutil.copy2(os.path.join(libcxx_libs, f), libcxx_install)


def build_crts(stage2_install, clang_version):
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')
    # Now build compiler-rt for each arch
    for (arch, llvm_triple, crt_defines,
         cflags) in cross_compile_configs(stage2_install):
        logger().info('Building compiler-rt for %s', arch)
        crt_path = utils.out_path('lib', 'clangrt-' + arch)
        crt_install = os.path.join(stage2_install, 'lib64', 'clang',
                                   clang_version.long_version())

        crt_defines['ANDROID'] = '1'
        crt_defines['LLVM_CONFIG_PATH'] = llvm_config
        crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # crt_defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'

        cflags.append('-isystem ' + support_headers())

        crt_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        crt_defines['CMAKE_ASM_FLAGS'] = ' '.join(cflags)
        crt_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        crt_defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = ' '.join(cflags)
        crt_defines['COMPILER_RT_TEST_TARGET_TRIPLE'] = llvm_triple
        crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        crt_defines['CMAKE_INSTALL_PREFIX'] = crt_install

        # Build libfuzzer separately.
        crt_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        crt_defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        if arch == 'arm':
          crt_defines['SANITIZER_COMMON_LINK_LIBS'] = '-latomic -landroid_support'
        else:
          crt_defines['SANITIZER_COMMON_LINK_LIBS'] = '-landroid_support'

        crt_defines.update(base_cmake_defines())

        crt_env = dict(ORIG_ENV)

        crt_cmake_path = utils.llvm_path('projects', 'compiler-rt')
        rm_cmake_cache(crt_path)
        invoke_cmake(
            out_path=crt_path,
            defines=crt_defines,
            env=crt_env,
            cmake_path=crt_cmake_path)


def build_libfuzzers(stage2_install, clang_version, ndk_cxx=False):
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')

    for (arch, llvm_triple, libfuzzer_defines, cflags) in cross_compile_configs(
            stage2_install, platform=(not ndk_cxx)):
        logger().info('Building libfuzzer for %s (ndk_cxx? %s)', arch, ndk_cxx)

        libfuzzer_path = utils.out_path('lib', 'libfuzzer-' + arch)
        if ndk_cxx:
            libfuzzer_path += '-ndk-cxx'

        libfuzzer_defines['ANDROID'] = '1'
        libfuzzer_defines['LLVM_CONFIG_PATH'] = llvm_config

        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        libfuzzer_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libfuzzer_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

        # lib/Fuzzer/CMakeLists.txt does not call cmake_minimum_required() to
        # set a minimum version.  Explicitly request a policy that'll pass
        # CMAKE_*_LINKER_FLAGS to the trycompile() step.
        libfuzzer_defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'

        libfuzzer_cmake_path = utils.llvm_path('projects', 'compiler-rt')
        libfuzzer_env = dict(ORIG_ENV)
        rm_cmake_cache(libfuzzer_path)
        invoke_cmake(
            out_path=libfuzzer_path,
            defines=libfuzzer_defines,
            env=libfuzzer_env,
            cmake_path=libfuzzer_cmake_path,
            target='fuzzer',
            install=False)
        # We need to install libfuzzer manually.
        sarch = arch
        if sarch == 'i386':
            sarch = 'i686'
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'
        static_lib = os.path.join(libfuzzer_path, 'lib', 'linux', static_lib_filename)
        triple_arch = arch_from_triple(llvm_triple)
        if ndk_cxx:
            lib_subdir = os.path.join('runtimes_ndk_cxx', triple_arch)
        else:
            lib_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        lib_dir = os.path.join(stage2_install, lib_subdir)

        check_create_path(lib_dir)
        shutil.copy2(static_lib, os.path.join(lib_dir, 'libFuzzer.a'))

    # Install libfuzzer headers.
    header_src = utils.llvm_path('projects', 'compiler-rt', 'lib', 'fuzzer')
    header_dst = os.path.join(stage2_install, 'prebuilt_include', 'llvm', 'lib',
                              'Fuzzer')
    check_create_path(header_dst)
    for f in os.listdir(header_src):
        if f.endswith('.h') or f.endswith('.def'):
            shutil.copy2(os.path.join(header_src, f), header_dst)


def build_libomp(stage2_install, clang_version, ndk_cxx=False):

    for (arch, llvm_triple, libomp_defines, cflags) in cross_compile_configs(
            stage2_install, platform=(not ndk_cxx)):

        logger().info('Building libomp for %s (ndk_cxx? %s)', arch, ndk_cxx)
        cflags.extend('-isystem ' + d for d in libcxx_header_dirs(ndk_cxx))

        libomp_path = utils.out_path('lib', 'libomp-' + arch)
        if ndk_cxx:
            libomp_path += '-ndk-cxx'

        libomp_defines['ANDROID'] = '1'
        libomp_defines['CMAKE_BUILD_TYPE'] = 'Release'
        libomp_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
        libomp_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)
        libomp_defines['LIBOMP_ENABLE_SHARED'] = 'FALSE'

        # Minimum version for OpenMP's CMake is too low for the CMP0056 policy
        # to be ON by default.
        libomp_defines['CMAKE_POLICY_DEFAULT_CMP0056'] = 'NEW'

        libomp_cmake_path = utils.llvm_path('projects', 'openmp', 'runtime')
        libomp_env = dict(ORIG_ENV)
        rm_cmake_cache(libomp_path)
        invoke_cmake(
            out_path=libomp_path,
            defines=libomp_defines,
            env=libomp_env,
            cmake_path=libomp_cmake_path,
            install=False)

        # We need to install libomp manually.
        static_lib = os.path.join(libomp_path, 'src', 'libomp.a')
        triple_arch = arch_from_triple(llvm_triple)
        if ndk_cxx:
            lib_subdir = os.path.join('runtimes_ndk_cxx', triple_arch)
        else:
            lib_subdir = clang_resource_dir(clang_version.long_version(),
                                            triple_arch)
        lib_dir = os.path.join(stage2_install, lib_subdir)

        check_create_path(lib_dir)
        shutil.copy2(static_lib, os.path.join(lib_dir, 'libomp.a'))


def build_crts_host_i686(stage2_install, clang_version):
    llvm_config = os.path.join(stage2_install, 'bin', 'llvm-config')

    crt_install = os.path.join(stage2_install, 'lib64', 'clang',
                               clang_version.long_version())
    crt_cmake_path = utils.llvm_path('projects', 'compiler-rt')

    crt_defines = base_cmake_defines()
    crt_defines['CMAKE_C_COMPILER'] = os.path.join(stage2_install, 'bin',
                                                   'clang')
    crt_defines['CMAKE_CXX_COMPILER'] = os.path.join(stage2_install, 'bin',
                                                     'clang++')

    # Skip building runtimes for i386
    crt_defines['COMPILER_RT_DEFAULT_TARGET_ONLY'] = 'ON'

    # Due to CMake and Clang oddities, we need to explicitly set
    # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
    # relying on auto-detection from the Compiler-rt CMake files.
    crt_defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'

    cflags = ['--target=i386-linux-gnu', "-march=i686"]
    crt_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    crt_defines['CMAKE_CXX_FLAGS'] = ' '.join(cflags)

    crt_defines['LLVM_CONFIG_PATH'] = llvm_config
    crt_defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
    crt_defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
    crt_defines['CMAKE_INSTALL_PREFIX'] = crt_install
    crt_defines['SANITIZER_CXX_ABI'] = 'libstdc++'

    crt_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

    crt_env = dict(ORIG_ENV)

    crt_path = utils.out_path('lib', 'clangrt-i386-host')
    rm_cmake_cache(crt_path)
    invoke_cmake(
        out_path=crt_path,
        defines=crt_defines,
        env=crt_env,
        cmake_path=crt_cmake_path)


def build_llvm(targets,
               build_dir,
               install_dir,
               build_name,
               extra_defines=None,
               extra_env=None):
    cmake_defines = base_cmake_defines()
    cmake_defines['CMAKE_INSTALL_PREFIX'] = install_dir
    cmake_defines['LLVM_TARGETS_TO_BUILD'] = targets
    cmake_defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'
    cmake_defines['CLANG_VENDOR'] = 'Android (' + build_name + ' based on ' + \
        android_version.svn_revision + ') '
    cmake_defines['LLVM_BINUTILS_INCDIR'] = utils.android_path(
        'toolchain/binutils/binutils-2.27/include')

    if extra_defines is not None:
        cmake_defines.update(extra_defines)

    env = dict(ORIG_ENV)
    if extra_env is not None:
        env.update(extra_env)

    invoke_cmake(
        out_path=build_dir,
        defines=cmake_defines,
        env=env,
        cmake_path=utils.llvm_path())


def build_llvm_for_windows(targets,
                           enable_assertions,
                           build_dir,
                           install_dir,
                           build_name,
                           native_clang_install,
                           is_32_bit=False):

    mingw_path = utils.android_path('prebuilts', 'gcc', 'linux-x86', 'host',
                                    'x86_64-w64-mingw32-4.8')
    mingw_cc = os.path.join(mingw_path, 'bin', 'x86_64-w64-mingw32-gcc')
    mingw_cxx = os.path.join(mingw_path, 'bin', 'x86_64-w64-mingw32-g++')

    # Write a NATIVE.cmake in windows_path that contains the compilers used
    # to build native tools such as llvm-tblgen and llvm-config.  This is
    # used below via the CMake variable CROSS_TOOLCHAIN_FLAGS_NATIVE.
    native_clang_cc = os.path.join(native_clang_install, 'bin', 'clang')
    native_clang_cxx = os.path.join(native_clang_install, 'bin', 'clang++')
    check_create_path(build_dir)
    native_cmake_file_path = os.path.join(build_dir, 'NATIVE.cmake')
    native_cmake_text = ('set(CMAKE_C_COMPILER {cc})\n'
                         'set(CMAKE_CXX_COMPILER {cxx})\n').format(
                             cc=native_clang_cc, cxx=native_clang_cxx)

    with open(native_cmake_file_path, 'w') as native_cmake_file:
        native_cmake_file.write(native_cmake_text)

    # Extra cmake defines to use while building for Windows
    windows_extra_defines = dict()
    windows_extra_defines['CMAKE_C_COMPILER'] = mingw_cc
    windows_extra_defines['CMAKE_CXX_COMPILER'] = mingw_cxx
    windows_extra_defines['CMAKE_SYSTEM_NAME'] = 'Windows'
    # Don't build compiler-rt, libcxx etc. for Windows
    windows_extra_defines['LLVM_BUILD_RUNTIME'] = 'OFF'
    # Build clang-tidy/clang-format for Windows.
    windows_extra_defines['LLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD'] = 'ON'
    windows_extra_defines['LLVM_TOOL_OPENMP_BUILD'] = 'OFF'

    windows_extra_defines['CROSS_TOOLCHAIN_FLAGS_NATIVE'] = \
        '-DCMAKE_PREFIX_PATH=' + cmake_prebuilt_bin_dir() + ';' + \
        '-DCMAKE_TOOLCHAIN_FILE=' + native_cmake_file_path

    if enable_assertions:
        windows_extra_defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'

    cflags = ['-D_LARGEFILE_SOURCE', '-D_FILE_OFFSET_BITS=64']
    cxxflags = list(cflags)
    # http://b/62787860 - mingw can't properly de-duplicate some functions
    # on 64-bit Windows builds. This mostly happens on builds without
    # assertions, because of llvm_unreachable() on functions that should
    # return a value (and control flow fallthrough - undefined behavior).
    ldflags = ['-Wl,--allow-multiple-definition']

    if is_32_bit:
        cflags.append('-m32')
        cxxflags.append('-m32')
        ldflags.append('-m32')

        # 32-bit libraries belong in lib/.
        windows_extra_defines['LLVM_LIBDIR_SUFFIX'] = ''

    windows_extra_defines['CMAKE_C_FLAGS'] = ' '.join(cflags)
    windows_extra_defines['CMAKE_CXX_FLAGS'] = ' '.join(cxxflags)
    windows_extra_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    windows_extra_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    windows_extra_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    build_llvm(
        targets=targets,
        build_dir=build_dir,
        install_dir=install_dir,
        build_name=build_name,
        extra_defines=windows_extra_defines)


def build_stage1(stage1_install, build_name, build_llvm_tools=False):
    # Build/install the stage 1 toolchain
    stage1_path = utils.out_path('stage1')
    stage1_targets = 'X86'

    stage1_extra_defines = dict()
    stage1_extra_defines['LLVM_BUILD_RUNTIME'] = 'ON'
    stage1_extra_defines['CLANG_ENABLE_ARCMT'] = 'OFF'
    stage1_extra_defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'
    stage1_extra_defines['CMAKE_C_COMPILER'] = os.path.join(
        clang_prebuilt_bin_dir(), 'clang')
    stage1_extra_defines['CMAKE_CXX_COMPILER'] = os.path.join(
        clang_prebuilt_bin_dir(), 'clang++')
    stage1_extra_defines['LLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD'] = 'OFF'
    stage1_extra_defines['LLVM_TOOL_OPENMP_BUILD'] = 'OFF'

    if build_llvm_tools:
        stage1_extra_defines['LLVM_BUILD_TOOLS'] = 'ON'
    else:
        stage1_extra_defines['LLVM_BUILD_TOOLS'] = 'OFF'

    # Have clang use libc++, ...
    stage1_extra_defines['LLVM_ENABLE_LIBCXX'] = 'ON'

    # ... and point CMake to the libc++.so from the prebuilts.  Install an rpath
    # to prevent linking with the newly-built libc++.so
    ldflags = ['-Wl,-rpath,' + clang_prebuilt_lib_dir()]
    stage1_extra_defines['CMAKE_EXE_LINKER_FLAGS'] = ' '.join(ldflags)
    stage1_extra_defines['CMAKE_SHARED_LINKER_FLAGS'] = ' '.join(ldflags)
    stage1_extra_defines['CMAKE_MODULE_LINKER_FLAGS'] = ' '.join(ldflags)

    # Make libc++.so a symlink to libc++.so.x instead of a linker script that
    # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
    # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
    if utils.host_is_linux():
        stage1_extra_defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
        stage1_extra_defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

    # Do not build compiler-rt for Darwin.  We don't ship host (or any
    # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
    # fail compilation of lib/builtins/atomic_*.c that only get built for
    # Darwin and fail compilation due to us using the bionic version of
    # stdatomic.h.
    if utils.host_is_darwin():
        stage1_extra_defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

    # Don't build libfuzzer, since it's broken on Darwin and we don't need it
    # anyway.
    stage1_extra_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

    build_llvm(
        targets=stage1_targets,
        build_dir=stage1_path,
        install_dir=stage1_install,
        build_name=build_name,
        extra_defines=stage1_extra_defines)


def build_stage2(stage1_install,
                 stage2_install,
                 stage2_targets,
                 build_name,
                 use_lld=False,
                 enable_assertions=False,
                 debug_build=False,
                 build_instrumented=False,
                 profdata_file=None):
    # TODO(srhines): Build LTO plugin (Chromium folks say ~10% perf speedup)

    # Build/install the stage2 toolchain
    stage2_cc = os.path.join(stage1_install, 'bin', 'clang')
    stage2_cxx = os.path.join(stage1_install, 'bin', 'clang++')
    stage2_path = utils.out_path('stage2')

    stage2_extra_defines = dict()
    stage2_extra_defines['CMAKE_C_COMPILER'] = stage2_cc
    stage2_extra_defines['CMAKE_CXX_COMPILER'] = stage2_cxx
    stage2_extra_defines['LLVM_BUILD_RUNTIME'] = 'ON'
    stage2_extra_defines['LLVM_ENABLE_LIBCXX'] = 'ON'
    stage2_extra_defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'

    # Don't build libfuzzer, since it's broken on Darwin and we don't need it
    # anyway.
    stage2_extra_defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

    if use_lld:
        stage2_extra_defines['LLVM_ENABLE_LLD'] = 'ON'

    if enable_assertions:
        stage2_extra_defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'

    if debug_build:
        stage2_extra_defines['CMAKE_BUILD_TYPE'] = 'Debug'

    if build_instrumented:
        stage2_extra_defines['LLVM_BUILD_INSTRUMENTED'] = 'ON'

        # llvm-profdata is only needed to finish CMake configuration
        # (tools/clang/utils/perf-training/CMakeLists.txt) and not needed for
        # build
        llvm_profdata = os.path.join(stage1_install, 'bin', 'llvm-profdata')
        stage2_extra_defines['LLVM_PROFDATA'] = llvm_profdata

        # libcxx, libcxxabi build with -nodefaultlibs and cannot link with
        # -fprofile-instr-generate because libclang_rt.profile depends on libc.
        # Skip building runtimes and use libstdc++.
        stage2_extra_defines['LLVM_ENABLE_LIBCXX'] = 'OFF'
        stage2_extra_defines['LLVM_BUILD_RUNTIME'] = 'OFF'

    if profdata_file:
        if build_instrumented:
            raise RuntimeError(
                'Cannot simultaneously instrument and use profiles')

        stage2_extra_defines['LLVM_PROFDATA_FILE'] = profdata_file

    # Make libc++.so a symlink to libc++.so.x instead of a linker script that
    # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
    # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
    if utils.host_is_linux():
        stage2_extra_defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        stage2_extra_defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'

    # Do not build compiler-rt for Darwin.  We don't ship host (or any
    # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
    # fail compilation of lib/builtins/atomic_*.c that only get built for
    # Darwin and fail compilation due to us using the bionic version of
    # stdatomic.h.
    if utils.host_is_darwin():
        stage2_extra_defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

    # Point CMake to the libc++ from stage1.  It is possible that once built,
    # the newly-built libc++ may override this because of the rpath pointing to
    # $ORIGIN/../lib64.  That'd be fine because both libraries are built from
    # the same sources.
    stage2_extra_env = dict()
    stage2_extra_env['LD_LIBRARY_PATH'] = os.path.join(stage1_install, 'lib64')

    build_llvm(
        targets=stage2_targets,
        build_dir=stage2_path,
        install_dir=stage2_install,
        build_name=build_name,
        extra_defines=stage2_extra_defines,
        extra_env=stage2_extra_env)


def build_runtimes(stage2_install):
    version = extract_clang_version(stage2_install)
    build_crts(stage2_install, version)
    build_crts_host_i686(stage2_install, version)
    build_libfuzzers(stage2_install, version)
    build_libfuzzers(stage2_install, version, ndk_cxx=True)
    build_libomp(stage2_install, version)
    build_libomp(stage2_install, version, ndk_cxx=True)
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(stage2_install, version)
    build_asan_test(stage2_install)
    build_asan_map_files(stage2_install, version)


def install_wrappers(llvm_install_path):
    wrapper_path = utils.llvm_path('android', 'compiler_wrapper.py')
    bisect_path = utils.llvm_path('android', 'bisect_driver.py')
    bin_path = os.path.join(llvm_install_path, 'bin')
    clang_path = os.path.join(bin_path, 'clang')
    clangxx_path = os.path.join(bin_path, 'clang++')
    clang_tidy_path = os.path.join(bin_path, 'clang-tidy')

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not os.path.exists(clang_path + '.real'):
        shutil.move(clang_path, clang_path + '.real')
    if not os.path.exists(clang_tidy_path + '.real'):
        shutil.move(clang_tidy_path, clang_tidy_path + '.real')
    utils.remove(clang_path)
    utils.remove(clangxx_path)
    utils.remove(clang_tidy_path)
    utils.remove(clangxx_path + '.real')
    os.symlink('clang.real', clangxx_path + '.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    install_file(bisect_path, bin_path)


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir, host, version):
    if host == 'linux-x86':
        libs = {'libLLVM': 'libLLVM-{version}svn.so',
                'libclang': 'libclang.so.{version}',
                'libc++': 'libc++.so.{version}',
                'libc++abi': 'libc++abi.so.{version}'
               }
    else:
        libs = {'libc++': 'libc++.{version}.dylib',
                'libc++abi': 'libc++abi.{version}.dylib'
               }

    def getVersions(libname):
        if not libname.startswith('libc++'):
            return version.short_version(), version.major
        else:
            return '1.0', '1'

    libdir = os.path.join(install_dir, 'lib64')
    for libname, libformat in libs.iteritems():
        short_version, major = getVersions(libname)

        real_lib = os.path.join(libdir, libformat.format(version=short_version))
        soname_lib = os.path.join(libdir, libformat.format(version=major))

        if libname == 'libLLVM':
            # Hack: soname_lib doesn't exist for LLVM.  No need to move
            soname_lib = real_lib
        else:
            # Rename the library to match its SONAME
            if not os.path.isfile(real_lib):
                raise RuntimeError(real_lib + ' must be a regular file')
            if not os.path.islink(soname_lib):
                raise RuntimeError(soname_lib + ' must be a symlink')

            shutil.move(real_lib, soname_lib)

        # Retain only soname_lib and delete other files for this library.
        all_libs = [lib for lib in os.listdir(libdir) if
                        lib.startswith(libname + '.') or # so libc++abi is ignored
                        lib.startswith(libname + '-')]
        for lib in all_libs:
            lib = os.path.join(libdir, lib)
            if lib != soname_lib:
                remove(lib)


def install_license_files(install_dir):
    projects = (
        'llvm',
        'llvm/projects/compiler-rt',
        'llvm/projects/libcxx',
        'llvm/projects/libcxxabi',
        'llvm/projects/openmp',
        'llvm/tools/clang',
        'llvm/tools/clang/tools/extra',
        'llvm/tools/lld',
    )

    # Get generic MODULE_LICENSE_* files from our android subdirectory.
    toolchain_path = utils.android_path('toolchain')
    llvm_android_path = os.path.join(toolchain_path, 'llvm', 'android')
    license_pattern = os.path.join(llvm_android_path, 'MODULE_LICENSE_*')
    for license_file in glob.glob(license_pattern):
        install_file(license_file, install_dir)

    # Fetch all the LICENSE.* files under our projects and append them into a
    # single NOTICE file for the resulting prebuilts.
    notices = []
    for project in projects:
        project_path = os.path.join(toolchain_path, project)
        license_pattern = os.path.join(project_path, 'LICENSE.*')
        for license_file in glob.glob(license_pattern):
            with open(license_file) as notice_file:
                notices.append(notice_file.read())
    with open(os.path.join(install_dir, 'NOTICE'), 'w') as notice_file:
        notice_file.write('\n'.join(notices))


def install_winpthreads(is_windows32, install_dir):
    """Installs the winpthreads runtime to the Windows bin directory."""
    lib_name = 'libwinpthread-1.dll'
    mingw_dir = utils.android_path(
        'prebuilts/gcc/linux-x86/host/x86_64-w64-mingw32-4.8',
        'x86_64-w64-mingw32')
    # Yes, this indeed may be found in bin/ because the executables are the
    # 64-bit version by default.
    pthread_dir = 'lib32' if is_windows32 else 'bin'
    lib_path = os.path.join(mingw_dir, pthread_dir, lib_name)

    lib_install = os.path.join(install_dir, 'bin', lib_name)
    install_file(lib_path, lib_install)


def remove_static_libraries(static_lib_dir):
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a'):
                static_library = os.path.join(static_lib_dir, lib_file)
                remove(static_library)


def package_toolchain(build_dir, build_name, host, dist_dir, strip=True):
    is_windows32 = host == 'windows-i386'
    is_windows64 = host == 'windows-x86'
    is_windows = is_windows32 or is_windows64
    is_linux = host == 'linux-x86'
    package_name = 'clang-' + build_name
    install_host_dir = utils.out_path('install', host)
    install_dir = os.path.join(install_host_dir, package_name)
    version = extract_clang_version(build_dir)

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if os.path.exists(install_host_dir):
        shutil.rmtree(install_host_dir)

    # First copy over the entire set of output objects.
    shutil.copytree(build_dir, install_dir, symlinks=True)

    ext = '.exe' if is_windows else ''
    shlib_ext = '.dll' if is_windows else '.so' if is_linux else '.dylib'

    # Next, we remove unnecessary binaries.
    necessary_bin_files = [
        'clang' + ext,
        'clang++' + ext,
        'clang-' + version.short_version() + ext,
        'clang-format' + ext,
        'clang-tidy' + ext,
        'git-clang-format',  # No extension here
        'ld.lld' + ext,
        'ld64.lld' + ext,
        'lld' + ext,
        'lld-link' + ext,
        'llvm-ar' + ext,
        'llvm-as' + ext,
        'llvm-cov' + ext,
        'llvm-dis' + ext,
        'llvm-link' + ext,
        'llvm-modextract' + ext,
        'llvm-nm' + ext,
        'llvm-profdata' + ext,
        'llvm-readobj' + ext,
        'llvm-symbolizer' + ext,
        'sancov' + ext,
        'sanstats' + ext,
        'scan-build' + ext,
        'scan-view' + ext,
        'LLVMgold' + shlib_ext,
    ]

    # scripts that should not be stripped
    script_bins = [
        'git-clang-format',
        'scan-build',
        'scan-view',
    ]

    bin_dir = os.path.join(install_dir, 'bin')
    bin_files = os.listdir(bin_dir)
    for bin_filename in bin_files:
        binary = os.path.join(bin_dir, bin_filename)
        if os.path.isfile(binary):
            if bin_filename not in necessary_bin_files:
                remove(binary)
            elif strip:
                if bin_filename not in script_bins:
                    check_call(['strip', binary])

    # Next, we remove unnecessary static libraries.
    if is_windows32:
        lib_dir = 'lib'
    else:
        lib_dir = 'lib64'
    remove_static_libraries(os.path.join(install_dir, lib_dir))

    # For Windows, add other relevant libraries.
    if is_windows:
        install_winpthreads(is_windows32, install_dir)

    if not is_windows:
        install_wrappers(install_dir)
        normalize_llvm_host_libs(install_dir, host, version)

    # Next, we copy over stdatomic.h from bionic.
    stdatomic_path = utils.android_path('bionic', 'libc', 'include',
                                        'stdatomic.h')
    resdir_top = os.path.join(install_dir, lib_dir, 'clang')
    header_path = os.path.join(resdir_top, version.long_version(), 'include')
    install_file(stdatomic_path, header_path)

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = os.path.join(install_dir, 'AndroidVersion.txt')
    with open(version_file_path, 'w') as version_file:
        version_file.write('{}\n'.format(version.long_version()))
        version_file.write('based on {}\n'.format(android_version.svn_revision))

    # Package up the resulting trimmed install/ directory.
    tarball_name = package_name + '-' + host
    package_path = os.path.join(dist_dir, tarball_name) + '.tar.bz2'
    logger().info('Packaging %s', package_path)
    args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
    check_call(args)


def parse_args():
    """Parses and returns command line arguments."""
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='Increase log level. Defaults to logging.INFO.')
    parser.add_argument(
        '--build-name', default='dev', help='Release name for the package.')

    parser.add_argument(
        '--use-lld',
        action='store_true',
        default=False,
        help='Use lld for linking (only affects stage2)')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Build debuggable Clang and LLVM tools (only affects stage2)')

    parser.add_argument(
        '--build-instrumented',
        action='store_true',
        default=False,
        help='Build LLVM tools with PGO instrumentation')

    # Options to skip build or packaging (can't skip both, or the script does
    # nothing).
    build_package_group = parser.add_mutually_exclusive_group()
    build_package_group.add_argument(
        '--skip-build',
        '-sb',
        action='store_true',
        default=False,
        help='Skip the build, and only do the packaging step')
    build_package_group.add_argument(
        '--skip-package',
        '-sp',
        action='store_true',
        default=False,
        help='Skip the packaging, and only do the build step')

    parser.add_argument(
        '--no-strip',
        action='store_true',
        default=False,
        help='Don\'t strip binaries/libraries')

    parser.add_argument(
        '--no-build-windows',
        action='store_true',
        default=False,
        help='Don\'t build toolchain for Windows')

    parser.add_argument(
        '--check-pgo-profile',
        action='store_true',
        default=False,
        help='Fail if expected PGO profile doesn\'t exist')

    return parser.parse_args()


def main():
    args = parse_args()
    do_build = not args.skip_build
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug
    need_windows = utils.host_is_linux() and not args.no_build_windows

    log_levels = [logging.INFO, logging.DEBUG]
    verbosity = min(args.verbose, len(log_levels) - 1)
    log_level = log_levels[verbosity]
    logging.basicConfig(level=log_level)

    stage1_install = utils.out_path('stage1-install')
    stage2_install = utils.out_path('stage2-install')
    windows32_install = utils.out_path('windows-i386-install')
    windows64_install = utils.out_path('windows-x86-install')

    # TODO(pirama): Once we have a set of prebuilts with lld, pass use_lld for
    # stage1 as well.
    if do_build:
        for install_dir in (stage2_install, windows32_install,
                            windows64_install):
            if os.path.exists(install_dir):
                utils.rm_tree(install_dir)

        instrumented = utils.host_is_linux() and args.build_instrumented

        build_stage1(stage1_install, args.build_name,
                     build_llvm_tools=instrumented)

        long_version = extract_clang_long_version(stage1_install)
        profdata = pgo_profdata_file(long_version)
        # Do not use PGO profiles if profdata file doesn't exist unless failure
        # is explicitly requested via --check-pgo-profile.
        if profdata is None and args.check_pgo_profile:
            raise RuntimeError('Profdata file does not exist for ' +
                               long_version)

        build_stage2(stage1_install, stage2_install, STAGE2_TARGETS,
                     args.build_name, args.use_lld, args.enable_assertions,
                     args.debug, instrumented, profdata)

    if do_build and utils.host_is_linux():
        build_runtimes(stage2_install)

    if do_build and need_windows:
        # Build single-stage clang for Windows
        windows_targets = STAGE2_TARGETS

        # Build 64-bit clang for Windows
        windows64_path = utils.out_path('windows-x86')
        build_llvm_for_windows(
            targets=windows_targets,
            enable_assertions=args.enable_assertions,
            build_dir=windows64_path,
            install_dir=windows64_install,
            build_name=args.build_name,
            native_clang_install=stage2_install)

        # Build 32-bit clang for Windows
        windows32_path = utils.out_path('windows-i386')
        build_llvm_for_windows(
            targets=windows_targets,
            enable_assertions=args.enable_assertions,
            build_dir=windows32_path,
            install_dir=windows32_install,
            build_name=args.build_name,
            native_clang_install=stage2_install,
            is_32_bit=True)

    if do_package:
        dist_dir = ORIG_ENV.get('DIST_DIR', utils.out_path())
        package_toolchain(
            stage2_install,
            args.build_name,
            utils.build_os_type(),
            dist_dir,
            strip=do_strip_host_package)

        if need_windows:
            package_toolchain(
                windows32_install,
                args.build_name,
                'windows-i386',
                dist_dir,
                strip=do_strip)
            package_toolchain(
                windows64_install,
                args.build_name,
                'windows-x86',
                dist_dir,
                strip=do_strip)

    return 0


if __name__ == '__main__':
    main()
