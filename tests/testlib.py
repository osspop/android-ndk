#
# Copyright (C) 2015 The Android Open Source Project
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
from __future__ import absolute_import
from __future__ import print_function

import difflib
import filecmp
import glob
import imp
import multiprocessing
import os
import posixpath
import re
import shutil
import subprocess

import build.lib.build_support
from ndk.workqueue import WorkQueue
import tests.ndk as ndk
import tests.util as util

# pylint: disable=no-self-use


def _get_jobs_arg():
    return '-j{}'.format(multiprocessing.cpu_count() * 2)


def _make_subtest_name(test, case):
    return '.'.join([test, case])


def _scan_test_suite(suite_dir, test_class, *args):
    tests = []
    for dentry in os.listdir(suite_dir):
        path = os.path.join(suite_dir, dentry)
        if os.path.isdir(path):
            tests.append(test_class.from_dir(path, *args))
    return tests


def _fixup_expected_failure(result, config, bug):
    if isinstance(result, Failure):
        return ExpectedFailure(result.test_name, config, bug)
    elif isinstance(result, Success):
        return UnexpectedSuccess(result.test_name, config, bug)
    else:  # Skipped, UnexpectedSuccess, or ExpectedFailure.
        return result


# TODO(danalbert): Split building and running into separate tasks.
# If we allowed the test build to queue additional jobs for running each
# subtest, we could parallelize within a test as well. It would also let us
# retrieve results from the queue as they come in so we can print them
# immediately.
def _run_test(suite, test, out_dir, test_filters):
    """Runs a given test according to the given filters.

    Args:
        suite: Name of the test suite the test belongs to.
        test: The test to be run.
        out_dir: Out directory for building tests.
        test_filters: Filters to apply when running tests.

    Returns: Tuple of (suite, [TestResult]).
    """
    if not test_filters.filter(test.name):
        return suite, []

    config = test.check_build_unsupported()
    if config is not None:
        message = 'test unsupported for {}'.format(config)
        return suite, [Skipped(test.name, message)]

    results = []
    config, bug = test.check_build_broken()
    for result in test.run(out_dir, test_filters):
        if config is not None:
            # We need to check change each pass/fail to either an
            # ExpectedFailure or an UnexpectedSuccess as necessary.
            result = _fixup_expected_failure(result, config, bug)
        results.append(result)
    return suite, results


class TestRunner(object):
    def __init__(self, printer):
        self.printer = printer
        self.tests = {}

    def add_suite(self, name, path, test_class, *args):
        if name in self.tests:
            raise KeyError('suite {} already exists'.format(name))
        self.tests[name] = _scan_test_suite(path, test_class, *args)

    def run(self, out_dir, test_filters):
        workqueue = WorkQueue()
        try:
            for suite, tests in self.tests.items():
                for test in tests:
                    workqueue.add_task(
                        _run_test, suite, test, out_dir, test_filters)

            results = {suite: [] for suite in self.tests.keys()}
            while not workqueue.finished():
                suite, test_results = workqueue.get_result()
                results[suite].extend(test_results)
                for result in test_results:
                    self.printer.print_result(result)
            return results
        finally:
            workqueue.terminate()
            workqueue.join()


class TestResult(object):
    def __init__(self, test_name):
        self.test_name = test_name

    def __repr__(self):
        return self.to_string(colored=False)

    def passed(self):
        raise NotImplementedError

    def failed(self):
        raise NotImplementedError

    def to_string(self, colored=False):
        raise NotImplementedError


class Failure(TestResult):
    def __init__(self, test_name, message):
        super(Failure, self).__init__(test_name)
        self.message = message

    def passed(self):
        return False

    def failed(self):
        return True

    def to_string(self, colored=False):
        label = util.maybe_color('FAIL', 'red', colored)
        return '{} {}: {}'.format(label, self.test_name, self.message)


class Success(TestResult):
    def passed(self):
        return True

    def failed(self):
        return False

    def to_string(self, colored=False):
        label = util.maybe_color('PASS', 'green', colored)
        return '{} {}'.format(label, self.test_name)


class Skipped(TestResult):
    def __init__(self, test_name, reason):
        super(Skipped, self).__init__(test_name)
        self.reason = reason

    def passed(self):
        return False

    def failed(self):
        return False

    def to_string(self, colored=False):
        label = util.maybe_color('SKIP', 'yellow', colored)
        return '{} {}: {}'.format(label, self.test_name, self.reason)


class ExpectedFailure(TestResult):
    def __init__(self, test_name, config, bug):
        super(ExpectedFailure, self).__init__(test_name)
        self.config = config
        self.bug = bug

    def passed(self):
        return True

    def failed(self):
        return False

    def to_string(self, colored=False):
        label = util.maybe_color('KNOWN FAIL', 'yellow', colored)
        return '{} {}: known failure for {} ({})'.format(
            label, self.test_name, self.config, self.bug)


class UnexpectedSuccess(TestResult):
    def __init__(self, test_name, config, bug):
        super(UnexpectedSuccess, self).__init__(test_name)
        self.config = config
        self.bug = bug

    def passed(self):
        return False

    def failed(self):
        return True

    def to_string(self, colored=False):
        label = util.maybe_color('SHOULD FAIL', 'red', colored)
        return '{} {}: unexpected success for {} ({})'.format(
            label, self.test_name, self.config, self.bug)


class Test(object):
    def __init__(self, name, test_dir):
        self.name = name
        self.test_dir = test_dir

    def get_test_config(self):
        return TestConfig.from_test_dir(self.test_dir)

    def run(self, out_dir, test_filters):
        raise NotImplementedError


class AwkTest(Test):
    def __init__(self, name, test_dir, script):
        super(AwkTest, self).__init__(name, test_dir)
        self.script = script

    @classmethod
    def from_dir(cls, test_dir):
        test_name = os.path.basename(test_dir)
        script_name = test_name + '.awk'
        script = os.path.join(ndk.NDK_ROOT, 'build/awk', script_name)
        if not os.path.isfile(script):
            msg = '{} missing test script: {}'.format(test_name, script)
            raise RuntimeError(msg)

        # Check that all of our test cases are valid.
        for test_case in glob.glob(os.path.join(test_dir, '*.in')):
            golden_path = re.sub(r'\.in$', '.out', test_case)
            if not os.path.isfile(golden_path):
                msg = '{} missing output: {}'.format(test_name, golden_path)
                raise RuntimeError(msg)
        return cls(test_name, test_dir, script)

    # Awk tests only run in a single configuration. Disabling them per ABI,
    # platform, or toolchain has no meaning. Stub out the checks.
    def check_build_broken(self):
        return None, None

    def check_build_unsupported(self):
        return None

    def run(self, out_dir, test_filters):
        for test_case in glob.glob(os.path.join(self.test_dir, '*.in')):
            golden_path = re.sub(r'\.in$', '.out', test_case)
            result = self.run_case(out_dir, test_case, golden_path,
                                   test_filters)
            if result is not None:
                yield result

    def run_case(self, out_dir, test_case, golden_out_path, test_filters):
        case_name = os.path.splitext(os.path.basename(test_case))[0]
        name = _make_subtest_name(self.name, case_name)

        if not test_filters.filter(name):
            return None

        # We need a subdirectory named for our test to handle the case where
        # multiple awk tests share names for test cases. If run simultaneously,
        # the outputs will collide.
        out_path = os.path.join(
            out_dir, 'awk', name, os.path.basename(golden_out_path))
        test_out_dir = os.path.dirname(out_path)
        if not os.path.exists(test_out_dir):
            os.makedirs(test_out_dir)

        with open(test_case, 'r') as test_in, open(out_path, 'w') as out_file:
            awk_path = ndk.get_tool('awk')
            print('{} -f {} < {} > {}'.format(
                awk_path, self.script, test_case, out_path))
            rc = subprocess.call([awk_path, '-f', self.script], stdin=test_in,
                                 stdout=out_file)
            if rc != 0:
                return Failure(name, 'awk failed')

        if filecmp.cmp(out_path, golden_out_path):
            return Success(name)
        else:
            with open(out_path) as out_file:
                out_lines = out_file.readlines()
            with open(golden_out_path) as golden_out_file:
                golden_lines = golden_out_file.readlines()
            diff = ''.join(difflib.unified_diff(
                golden_lines, out_lines, fromfile='expected', tofile='actual'))
            message = 'output does not match expected:\n\n' + diff
            return Failure(name, message)


def _prep_build_dir(src_dir, out_dir):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(src_dir, out_dir)


class TestConfig(object):
    """Describes the status of a test.

    Each test directory can contain a "test_config.py" file that describes
    the configurations a test is not expected to pass for. Previously this
    information could be captured in one of two places: the Application.mk
    file, or a BROKEN_BUILD/BROKEN_RUN file.

    Application.mk was used to state that a test was only to be run for a
    specific platform version, specific toolchain, or a set of ABIs.
    Unfortunately Application.mk could only specify a single toolchain or
    platform, not a set.

    BROKEN_BUILD/BROKEN_RUN files were too general. An empty file meant the
    test should always be skipped regardless of configuration. Any change that
    would put a test in that situation should be reverted immediately. These
    also didn't make it clear if the test was actually broken (and thus should
    be fixed) or just not applicable.

    A test_config.py file is more flexible. It is a Python module that defines
    at least one function by the same name as one in TestConfig.NullTestConfig.
    If a function is not defined the null implementation (not broken,
    supported), will be used.
    """

    class NullTestConfig(object):
        def __init__(self):
            pass

        # pylint: disable=unused-argument
        @staticmethod
        def build_broken(abi, platform, toolchain):
            """Tests if a given configuration is known broken.

            A broken test is a known failing test that should be fixed.

            Any test with a non-empty broken section requires a "bug" entry
            with a link to either an internal bug (http://b/BUG_NUMBER) or a
            public bug (http://b.android.com/BUG_NUMBER).

            These tests will still be built and run. If the test succeeds, it
            will be reported as an error.

            Returns: A tuple of (broken_configuration, bug) or (None, None).
            """
            return None, None

        @staticmethod
        def build_unsupported(abi, platform, toolchain):
            """Tests if a given configuration is unsupported.

            An unsupported test is a test that do not make sense to run for a
            given configuration. Testing x86 assembler on MIPS, for example.

            These tests will not be built or run.

            Returns: The string unsupported_configuration or None.
            """
            return None

        @staticmethod
        def extra_cmake_flags():
            return []
        # pylint: enable=unused-argument

    def __init__(self, file_path):
        # Note that this namespace isn't actually meaningful from our side;
        # it's only what the loaded module's __name__ gets set to.
        dirname = os.path.dirname(file_path)
        namespace = '.'.join([dirname, 'test_config'])

        try:
            self.module = imp.load_source(namespace, file_path)
        except IOError:
            self.module = None

        try:
            self.build_broken = self.module.build_broken
        except AttributeError:
            self.build_broken = self.NullTestConfig.build_broken

        try:
            self.build_unsupported = self.module.build_unsupported
        except AttributeError:
            self.build_unsupported = self.NullTestConfig.build_unsupported

        try:
            self.extra_cmake_flags = self.module.extra_cmake_flags
        except AttributeError:
            self.extra_cmake_flags = self.NullTestConfig.extra_cmake_flags

    @classmethod
    def from_test_dir(cls, test_dir):
        path = os.path.join(test_dir, 'test_config.py')
        return cls(path)


class DeviceTestConfig(TestConfig):
    """Specialization of test_config.py that includes device API level.

    We need to mark some tests as broken or unsupported based on what device
    they are running on, as opposed to just what they were built for.
    """
    class NullTestConfig(TestConfig.NullTestConfig):
        # pylint: disable=unused-argument
        @staticmethod
        def run_broken(abi, device_api, toolchain, subtest):
            return None, None

        @staticmethod
        def run_unsupported(abi, device_api, toolchain, subtest):
            return None

        @staticmethod
        def extra_cmake_flags():
            return []
        # pylint: enable=unused-argument

    def __init__(self, file_path):
        super(DeviceTestConfig, self).__init__(file_path)

        try:
            self.run_broken = self.module.run_broken
        except AttributeError:
            self.run_broken = self.NullTestConfig.run_broken

        try:
            self.run_unsupported = self.module.run_unsupported
        except AttributeError:
            self.run_unsupported = self.NullTestConfig.run_unsupported


def _run_build_sh_test(test_name, build_dir, test_dir, ndk_build_flags, abi,
                       platform, toolchain):
    _prep_build_dir(test_dir, build_dir)
    with util.cd(build_dir):
        build_cmd = ['bash', 'build.sh', _get_jobs_arg()] + ndk_build_flags
        test_env = dict(os.environ)
        if abi is not None:
            test_env['APP_ABI'] = abi
        test_env['APP_PLATFORM'] = 'android-{}'.format(platform)
        assert toolchain is not None
        test_env['NDK_TOOLCHAIN_VERSION'] = toolchain
        rc, out = util.call_output(build_cmd, env=test_env)
        if rc == 0:
            return Success(test_name)
        else:
            return Failure(test_name, out)


def _run_ndk_build_test(test_name, build_dir, test_dir, ndk_build_flags, abi,
                        platform, toolchain):
    _prep_build_dir(test_dir, build_dir)
    with util.cd(build_dir):
        args = [
            'APP_ABI=' + abi,
            'NDK_TOOLCHAIN_VERSION=' + toolchain,
            _get_jobs_arg(),
        ]
        if platform is not None:
            args.append('APP_PLATFORM=android-{}'.format(platform))
        rc, out = ndk.build(ndk_build_flags + args)
        if rc == 0:
            return Success(test_name)
        else:
            return Failure(test_name, out)


def _run_cmake_build_test(test_name, build_dir, test_dir, cmake_flags, abi,
                          platform, toolchain):
    _prep_build_dir(test_dir, build_dir)

    # Add prebuilts to PATH.
    prebuilts_host_tag = build.lib.build_support.get_default_host() + '-x86'
    prebuilts_bin = build.lib.build_support.android_path(
        'prebuilts', 'cmake', prebuilts_host_tag, 'bin')
    env = dict(os.environ)
    env['PATH'] = prebuilts_bin + os.pathsep + os.environ['PATH']

    # Skip if we don't have a working cmake executable, either from the
    # prebuilts, or from the SDK, or if a new enough version is installed.
    rc, out = util.call_output(['cmake', '--version'], env=env)
    if rc != 0:
        return Skipped(test_name, 'cmake executable not found')
    version_pattern = r'cmake version (\d+)\.(\d+)\.'
    version = [int(v) for v in re.match(version_pattern, out).groups()]
    if version < [3, 6]:
        return Skipped(test_name, 'cmake 3.6 or above required')

    toolchain_file = os.path.join(os.environ['NDK'], 'build', 'cmake',
                                  'android.toolchain.cmake')
    objs_dir = os.path.join(build_dir, 'objs', abi)
    libs_dir = os.path.join(build_dir, 'libs', abi)
    if toolchain != 'clang':
        toolchain = 'gcc'
    args = [
        '-H' + build_dir,
        '-B' + objs_dir,
        '-DCMAKE_TOOLCHAIN_FILE=' + toolchain_file,
        '-DANDROID_ABI=' + abi,
        '-DANDROID_TOOLCHAIN=' + toolchain,
        '-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=' + libs_dir,
        '-DCMAKE_LIBRARY_OUTPUT_DIRECTORY=' + libs_dir
    ]
    rc, _ = util.call_output(['ninja', '--version'], env=env)
    if rc == 0:
        args += [
            '-GNinja',
            '-DCMAKE_MAKE_PROGRAM=ninja',
        ]
    if platform is not None:
        args.append('-DANDROID_PLATFORM=android-{}'.format(platform))
    rc, out = util.call_output(['cmake'] + cmake_flags + args, env=env)
    if rc != 0:
        return Failure(test_name, out)
    rc, out = util.call_output(['cmake', '--build', objs_dir,
                                '--', _get_jobs_arg()], env=env)
    if rc != 0:
        return Failure(test_name, out)
    return Success(test_name)


class BuildTest(Test):
    def __init__(self, name, test_dir, abi, platform, toolchain,
                 ndk_build_flags=None, cmake_flags=None):
        super(BuildTest, self).__init__(name, test_dir)

        if ndk_build_flags is None:
            ndk_build_flags = []
        if cmake_flags is None:
            cmake_flags = []

        if platform is None:
            raise ValueError

        self.abi = abi
        self.platform = platform
        self.toolchain = toolchain
        self.ndk_build_flags = ndk_build_flags
        self.cmake_flags = cmake_flags + self.get_extra_cmake_flags()

    def run(self, out_dir, _):
        raise NotImplementedError

    @classmethod
    def from_dir(cls, test_dir, abi, platform, toolchain, ndk_build_flags,
                 cmake_flags):
        test_name = os.path.basename(test_dir)

        if os.path.isfile(os.path.join(test_dir, 'test.py')):
            return PythonBuildTest(test_name, test_dir, abi, platform,
                                   toolchain, ndk_build_flags)
        elif os.path.isfile(os.path.join(test_dir, 'build.sh')):
            return ShellBuildTest(test_name, test_dir, abi, platform,
                                  toolchain, ndk_build_flags)
        elif os.path.isfile(os.path.join(test_dir, 'CMakeLists.txt')):
            return CMakeBuildTest(test_name, test_dir, abi, platform,
                                  toolchain, cmake_flags)
        else:
            return NdkBuildTest(test_name, test_dir, abi, platform,
                                toolchain, ndk_build_flags)

    def check_build_broken(self):
        return self.get_test_config().build_broken(
            self.abi, self.platform, self.toolchain)

    def check_build_unsupported(self):
        return self.get_test_config().build_unsupported(
            self.abi, self.platform, self.toolchain)

    def get_extra_cmake_flags(self):
        return self.get_test_config().extra_cmake_flags()


class PythonBuildTest(BuildTest):
    """A test that is implemented by test.py.

    A test.py test has a test.py file in its root directory. This module
    contains a run_test function which returns a tuple of `(boolean_success,
    string_failure_message)` and takes the following kwargs (all of which
    default to None):

    abi: ABI to test as a string.
    platform: Platform to build against as a string.
    toolchain: Toolchain to use as a string.
    ndk_build_flags: Additional build flags that should be passed to ndk-build
                     if invoked as a list of strings.
    """
    def __init__(self, name, test_dir, abi, platform, toolchain,
                 ndk_build_flags):
        if platform is None:
            platform = build.lib.build_support.minimum_platform_level(abi)
        super(PythonBuildTest, self).__init__(
            name, test_dir, abi, platform, toolchain,
            ndk_build_flags=ndk_build_flags)

    def run(self, out_dir, _):
        build_dir = os.path.join(out_dir, self.name)
        print('Building test: {}'.format(self.name))
        _prep_build_dir(self.test_dir, build_dir)
        with util.cd(build_dir):
            module = imp.load_source('test', 'test.py')
            success, failure_message = module.run_test(
                abi=self.abi, platform=self.platform, toolchain=self.toolchain,
                build_flags=self.ndk_build_flags)
            if success:
                yield Success(self.name)
            else:
                yield Failure(self.name, failure_message)


class ShellBuildTest(BuildTest):
    def __init__(self, name, test_dir, abi, platform, toolchain,
                 ndk_build_flags):
        if platform is None:
            platform = build.lib.build_support.minimum_platform_level(abi)
        super(ShellBuildTest, self).__init__(
            name, test_dir, abi, platform, toolchain, ndk_build_flags)

    def run(self, out_dir, _):
        build_dir = os.path.join(out_dir, self.name)
        print('Building test: {}'.format(self.name))
        if os.name == 'nt':
            reason = 'build.sh tests are not supported on Windows'
            yield Skipped(self.name, reason)
        else:
            yield _run_build_sh_test(self.name, build_dir, self.test_dir,
                                     self.ndk_build_flags, self.abi,
                                     self.platform, self.toolchain)


def _platform_from_application_mk(test_dir):
    """Determine target API level from a test's Application.mk.

    Args:
        test_dir: Directory of the test to read.

    Returns:
        Integer portion of APP_PLATFORM if found, else None.

    Raises:
        ValueError: Found an unexpected value for APP_PLATFORM.
    """
    application_mk = os.path.join(test_dir, 'jni/Application.mk')
    if not os.path.exists(application_mk):
        return None

    with open(application_mk) as application_mk_file:
        for line in application_mk_file:
            if line.startswith('APP_PLATFORM'):
                _, platform_str = line.split(':=')
                break
        else:
            return None

    platform_str = platform_str.strip()
    if not platform_str.startswith('android-'):
        raise ValueError(platform_str)

    _, api_level_str = platform_str.split('-')
    return int(api_level_str)


def _get_or_infer_app_platform(platform_from_user, test_dir, abi):
    """Determines the platform level to use for a test using ndk-build.

    Choose the platform level from, in order of preference:
    1. Value given as argument.
    2. APP_PLATFORM from jni/Application.mk.
    3. Default value for the target ABI.

    Args:
        platform_from_user: A user provided platform level or None.
        test_dir: The directory containing the ndk-build project.
        abi: The ABI being targeted.

    Returns:
        The platform version the test should build against.
    """
    if platform_from_user is not None:
        return platform_from_user

    platform_from_application_mk = _platform_from_application_mk(test_dir)
    if platform_from_application_mk is not None:
        return platform_from_application_mk

    return build.lib.build_support.minimum_platform_level(abi)


class NdkBuildTest(BuildTest):
    def __init__(self, name, test_dir, abi, platform, toolchain,
                 ndk_build_flags):
        platform = _get_or_infer_app_platform(platform, test_dir, abi)
        super(NdkBuildTest, self).__init__(
            name, test_dir, abi, platform, toolchain, ndk_build_flags)

    def run(self, out_dir, _):
        build_dir = os.path.join(out_dir, self.name)
        print('Building test: {}'.format(self.name))
        yield _run_ndk_build_test(self.name, build_dir, self.test_dir,
                                  self.ndk_build_flags, self.abi,
                                  self.platform, self.toolchain)


class CMakeBuildTest(BuildTest):
    def __init__(self, name, test_dir, abi, platform, toolchain, cmake_flags):
        platform = _get_or_infer_app_platform(platform, test_dir, abi)
        super(CMakeBuildTest, self).__init__(
            name, test_dir, abi, platform, toolchain, cmake_flags=cmake_flags)

    def run(self, out_dir, _):
        build_dir = os.path.join(out_dir, self.name)
        print('Building test: {}'.format(self.name))
        yield _run_cmake_build_test(self.name, build_dir, self.test_dir,
                                    self.cmake_flags, self.abi,
                                    self.platform, self.toolchain)


def _copy_test_to_device(device, build_dir, device_dir, abi, test_filters,
                         test_name):
    abi_dir = os.path.join(build_dir, 'libs', abi)
    if not os.path.isdir(abi_dir):
        raise RuntimeError('No libraries for {}'.format(abi))

    test_cases = []
    for test_file in os.listdir(abi_dir):
        if test_file in ('gdbserver', 'gdb.setup'):
            continue

        file_is_lib = True
        if not test_file.endswith('.so'):
            file_is_lib = False
            case_name = _make_subtest_name(test_name, test_file)
            if not test_filters.filter(case_name):
                continue
            test_cases.append(test_file)

        # TODO(danalbert): Libs with the same name will clobber each other.
        # This was the case with the old shell based script too. I'm trying not
        # to change too much in the translation.
        lib_path = os.path.join(abi_dir, test_file)
        print('Pushing {} to {}...'.format(lib_path, device_dir))
        device.push(lib_path, device_dir)

        # Binaries pushed from Windows may not have execute permissions.
        if not file_is_lib:
            file_path = posixpath.join(device_dir, test_file)
            # Can't use +x because apparently old versions of Android didn't
            # support that...
            device.shell(['chmod', '777', file_path])

        # TODO(danalbert): Sync data.
        # The libc++ tests contain a DATA file that lists test names and their
        # dependencies on file system data. These files need to be copied to
        # the device.

    if len(test_cases) == 0:
        raise RuntimeError('Could not find any test executables.')

    return test_cases


class DeviceTest(Test):
    def __init__(self, name, test_dir, abi, platform, device, device_platform,
                 toolchain, ndk_build_flags, cmake_flags, skip_run):
        super(DeviceTest, self).__init__(name, test_dir)

        platform = _get_or_infer_app_platform(platform, test_dir, abi)

        self.abi = abi
        self.platform = platform
        self.device = device
        self.device_platform = device_platform
        self.toolchain = toolchain
        self.ndk_build_flags = ndk_build_flags
        self.cmake_flags = cmake_flags + self.get_extra_cmake_flags()
        self.skip_run = skip_run

    @classmethod
    def from_dir(cls, test_dir, abi, platform, device, device_platform,
                 toolchain, ndk_build_flags, cmake_flags, skip_run):
        test_name = os.path.basename(test_dir)
        return cls(test_name, test_dir, abi, platform, device, device_platform,
                   toolchain, ndk_build_flags, cmake_flags, skip_run)

    def get_test_config(self):
        return DeviceTestConfig.from_test_dir(self.test_dir)

    def check_build_broken(self):
        return self.get_test_config().build_broken(
            self.abi, self.platform, self.toolchain)

    def check_build_unsupported(self):
        return self.get_test_config().build_unsupported(
            self.abi, self.platform, self.toolchain)

    def check_run_broken(self, subtest):
        return self.get_test_config().run_broken(
            self.abi, self.device_platform, self.toolchain, subtest)

    def check_run_unsupported(self, subtest):
        if self.platform > self.device_platform:
            return 'device platform {} < build platform {}'.format(
                self.device_platform, self.platform)
        return self.get_test_config().run_unsupported(
            self.abi, self.device_platform, self.toolchain, subtest)

    def get_extra_cmake_flags(self):
        return self.get_test_config().extra_cmake_flags()

    def run_ndk_build(self, out_dir, test_filters):
        build_dir = os.path.join(out_dir, self.name)
        build_result = _run_ndk_build_test(self.name, build_dir, self.test_dir,
                                           self.ndk_build_flags, self.abi,
                                           self.platform, self.toolchain)
        if not build_result.passed():
            yield build_result
            return

        if self.skip_run:
            yield build_result
            return

        for result in self.run_device_test(build_dir, 'ndk-tests',
                                           test_filters):
            yield result

    def run_cmake_build(self, out_dir, test_filters):
        build_dir = os.path.join(out_dir, self.name)
        build_result = _run_cmake_build_test(self.name, build_dir,
                                             self.test_dir, self.cmake_flags,
                                             self.abi, self.platform,
                                             self.toolchain)
        if not build_result.passed():
            yield build_result
            return

        if self.skip_run:
            yield build_result
            return

        for result in self.run_device_test(build_dir, 'cmake-tests',
                                           test_filters):
            yield result

    def run_device_test(self, build_dir, test_dir, test_filters):
        device_dir = posixpath.join('/data/local/tmp', test_dir, self.name)

        # We have to use `ls foo || mkdir foo` because Gingerbread was lacking
        # `mkdir -p`, the -d check for directory existence, stat, dirname, and
        # every other thing I could think of to implement this aside from ls.
        self.device.shell(['ls {0} || mkdir {0}'.format(device_dir)])

        try:
            test_cases = _copy_test_to_device(
                self.device, build_dir, device_dir, self.abi, test_filters,
                self.name)
            for case in test_cases:
                case_name = _make_subtest_name(self.name, case)
                if not test_filters.filter(case_name):
                    continue

                config = self.check_run_unsupported(case)
                if config is not None:
                    message = 'test unsupported for {}'.format(config)
                    yield Skipped(case_name, message)
                    continue

                cmd = 'cd {} && LD_LIBRARY_PATH={} ./{} 2>&1'.format(
                    device_dir, device_dir, case)
                result, out, _ = self.device.shell_nocheck([cmd])

                config, bug = self.check_run_broken(case)
                if config is None:
                    if result == 0:
                        yield Success(case_name)
                    else:
                        yield Failure(case_name, out)
                else:
                    if result == 0:
                        yield UnexpectedSuccess(case_name, config, bug)
                    else:
                        yield ExpectedFailure(case_name, config, bug)
        finally:
            self.device.shell_nocheck(['rm', '-r', device_dir])

    def run(self, out_dir, test_filters):
        if os.path.exists(os.path.join(self.test_dir, 'jni', 'Android.mk')):
            print('Building device test with ndk-build: {}'.format(self.name))
            for result in self.run_ndk_build(out_dir, test_filters):
                yield result
        if os.path.exists(os.path.join(self.test_dir, 'CMakeLists.txt')):
            print('Building device test with cmake: {}'.format(self.name))
            for result in self.run_cmake_build(out_dir, test_filters):
                yield result
