#
# Copyright (C) 2017 The Android Open Source Project
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
"""APIs for enumerating and building NDK tests."""
from __future__ import absolute_import

import logging
import os
from pathlib import Path
import pickle
import random
import shutil
import sys
import traceback
from typing import (
    Dict,
    List,
    Tuple,
)

import ndk.abis
import ndk.archive
from ndk.test.buildtest.case import Test
from ndk.test.buildtest.scanner import TestScanner
from ndk.test.filters import TestFilter
from ndk.test.printers import Printer
from ndk.test.report import Report
import ndk.test.spec
import ndk.test.suites
import ndk.test.ui
from ndk.workqueue import LoadRestrictingWorkQueue, Worker


def logger() -> logging.Logger:
    """Returns the module logger."""
    return logging.getLogger(__name__)


def write_build_report(build_report: str, results: Report) -> None:
    with open(build_report, "wb") as build_report_file:
        pickle.dump(results, build_report_file)


def scan_test_suite(suite_dir: Path, test_scanner: TestScanner) -> List[Test]:
    tests: List[Test] = []
    for dentry in os.listdir(suite_dir):
        path = suite_dir / dentry
        if path.is_dir():
            test_name = path.name
            tests.extend(test_scanner.find_tests(path, test_name))
    return tests


def _fixup_expected_failure(
    result: ndk.test.result.TestResult, config: str, bug: str
) -> ndk.test.result.TestResult:
    if isinstance(result, ndk.test.result.Failure):
        return ndk.test.result.ExpectedFailure(result.test, result.message, config, bug)
    if isinstance(result, ndk.test.result.Success):
        return ndk.test.result.UnexpectedSuccess(result.test, config, bug)
    # Skipped, UnexpectedSuccess, or ExpectedFailure.
    return result


def _fixup_negative_test(
    result: ndk.test.result.TestResult,
) -> ndk.test.result.TestResult:
    if isinstance(result, ndk.test.result.Failure):
        return ndk.test.result.Success(result.test)
    if isinstance(result, ndk.test.result.Success):
        return ndk.test.result.Failure(result.test, "negative test case succeeded")
    # Skipped, UnexpectedSuccess, or ExpectedFailure.
    return result


RunTestResult = tuple[str, ndk.test.result.TestResult, list[Test]]


def _run_test(
    worker: Worker,
    suite: str,
    test: Test,
    obj_dir: Path,
    dist_dir: Path,
    test_filters: TestFilter,
) -> RunTestResult:
    """Runs a given test according to the given filters.

    Args:
        worker: The worker that invoked this task.
        suite: Name of the test suite the test belongs to.
        test: The test to be run.
        obj_dir: Out directory for intermediate build artifacts.
        dist_dir: Out directory for build artifacts needed for running.
        test_filters: Filters to apply when running tests.

    Returns: Tuple of (suite, TestResult, [Test]). The [Test] element is a list
             of additional tests to be run.
    """
    worker.status = "Building {}".format(test)

    config = test.check_unsupported()
    if config is not None:
        message = "test unsupported for {}".format(config)
        return suite, ndk.test.result.Skipped(test, message), []

    try:
        result, additional_tests = test.run(obj_dir, dist_dir, test_filters)
        if test.is_negative_test():
            result = _fixup_negative_test(result)
        config, bug = test.check_broken()
        if config is not None:
            # We need to check change each pass/fail to either an
            # ExpectedFailure or an UnexpectedSuccess as necessary.
            assert bug is not None
            result = _fixup_expected_failure(result, config, bug)
    except Exception:  # pylint: disable=broad-except
        result = ndk.test.result.Failure(test, traceback.format_exc())
        additional_tests = []
    return suite, result, additional_tests


class TestBuilder:
    def __init__(
        self,
        test_spec: ndk.test.spec.TestSpec,
        test_options: ndk.test.spec.TestOptions,
        printer: Printer,
    ) -> None:
        self.printer = printer
        self.tests: Dict[str, List[Test]] = {}
        self.build_dirs: Dict[Path, Tuple[str, Test]] = {}

        self.test_options = test_options

        self.obj_dir = self.test_options.out_dir / "obj"
        self.dist_dir = self.test_options.out_dir / "dist"

        self.find_tests(test_spec)

    def find_tests(self, test_spec: ndk.test.spec.TestSpec) -> None:
        scanner = ndk.test.buildtest.scanner.BuildTestScanner(
            self.test_options.ndk_path
        )
        nodist_scanner = ndk.test.buildtest.scanner.BuildTestScanner(
            self.test_options.ndk_path, dist=False
        )
        libcxx_scanner = ndk.test.buildtest.scanner.LibcxxTestScanner(
            self.test_options.ndk_path
        )
        build_api_level = None  # Always use the default.
        for abi in test_spec.abis:
            for toolchain_file in ndk.test.spec.CMakeToolchainFile:
                config = ndk.test.spec.BuildConfiguration(
                    abi, build_api_level, toolchain_file
                )
                scanner.add_build_configuration(config)
                nodist_scanner.add_build_configuration(config)
                libcxx_scanner.add_build_configuration(config)

        if "build" in test_spec.suites:
            test_src = self.test_options.src_dir / "build"
            self.add_suite("build", test_src, nodist_scanner)
        if "device" in test_spec.suites:
            test_src = self.test_options.src_dir / "device"
            self.add_suite("device", test_src, scanner)
        if "libc++" in test_spec.suites:
            test_src = self.test_options.src_dir / "libc++"
            self.add_suite("libc++", test_src, libcxx_scanner)

    def add_suite(self, name: str, path: Path, test_scanner: TestScanner) -> None:
        if name in self.tests:
            raise KeyError("suite {} already exists".format(name))
        new_tests = scan_test_suite(path, test_scanner)
        self.check_no_overlapping_build_dirs(name, new_tests)
        self.tests[name] = new_tests

    def check_no_overlapping_build_dirs(
        self, suite: str, new_tests: List[Test]
    ) -> None:
        for test in new_tests:
            build_dir = test.get_build_dir(Path(""))
            if build_dir in self.build_dirs:
                dup_suite, dup_test = self.build_dirs[build_dir]
                raise RuntimeError(
                    "Found duplicate build directory:\n{} {}\n{} {}".format(
                        dup_suite, dup_test, suite, test
                    )
                )
            self.build_dirs[build_dir] = (suite, test)

    def make_out_dirs(self) -> None:
        if not self.obj_dir.exists():
            self.obj_dir.mkdir(parents=True)
        if not self.dist_dir.exists():
            self.dist_dir.mkdir(parents=True)

    def clean_out_dir(self) -> None:
        if self.test_options.out_dir.exists():
            shutil.rmtree(self.test_options.out_dir)

    def build(self) -> Report:
        if self.test_options.clean:
            self.clean_out_dir()
        self.make_out_dirs()

        test_filters = TestFilter.from_string(self.test_options.test_filter)
        result = self.do_build(test_filters)
        if self.test_options.build_report:
            write_build_report(self.test_options.build_report, result)
        if result.successful and self.test_options.package_path is not None:
            self.package()
        return result

    def do_build(self, test_filters: TestFilter) -> Report:
        workqueue: LoadRestrictingWorkQueue[RunTestResult] = LoadRestrictingWorkQueue()
        try:
            for suite, tests in self.tests.items():
                # Each test configuration was expanded when each test was
                # discovered, so the current order has all the largest tests
                # right next to each other. Spread them out to try to avoid
                # having too many heavy builds happening simultaneously.
                random.shuffle(tests)
                for test in tests:
                    if not test_filters.filter(test.name):
                        continue

                    if test.name == "libc++":
                        workqueue.add_load_restricted_task(
                            _run_test,
                            suite,
                            test,
                            self.obj_dir,
                            self.dist_dir,
                            test_filters,
                        )
                    else:
                        workqueue.add_task(
                            _run_test,
                            suite,
                            test,
                            self.obj_dir,
                            self.dist_dir,
                            test_filters,
                        )

            report = Report()
            self.wait_for_results(report, workqueue, test_filters)

            return report
        finally:
            workqueue.terminate()
            workqueue.join()

    def wait_for_results(
        self,
        report: Report,
        workqueue: LoadRestrictingWorkQueue[RunTestResult],
        test_filters: TestFilter,
    ) -> None:
        console = ndk.ansi.get_console()
        ui = ndk.test.ui.get_test_build_progress_ui(console, workqueue)
        with ndk.ansi.disable_terminal_echo(sys.stdin):
            with console.cursor_hide_context():
                while not workqueue.finished():
                    for suite, result, additional_tests in workqueue.get_results():
                        assert result.passed() or not additional_tests
                        for test in additional_tests:
                            workqueue.add_task(
                                _run_test,
                                suite,
                                test,
                                self.obj_dir,
                                self.dist_dir,
                                test_filters,
                            )
                        if logger().isEnabledFor(logging.INFO):
                            ui.clear()
                            self.printer.print_result(result)
                        elif result.failed():
                            ui.clear()
                            self.printer.print_result(result)
                        report.add_result(suite, result)
                    ui.draw()
                ui.clear()

    def package(self) -> None:
        assert self.test_options.package_path is not None
        print("Packaging tests...")
        ndk.archive.make_bztar(
            self.test_options.package_path,
            self.test_options.out_dir.parent,
            Path("tests/dist"),
        )
