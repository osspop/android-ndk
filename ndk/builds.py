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
"""Defines the NDK build system API.

Note: this isn't the ndk-build API, but the API for building the NDK itself.
"""
from __future__ import annotations

# pylint: disable=import-error,no-name-in-module
# https://github.com/PyCQA/pylint/issues/73
from distutils.dir_util import copy_tree
from enum import auto, Enum, unique
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import textwrap
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from ndk.abis import Arch, ALL_ARCHITECTURES
from ndk.autoconf import AutoconfBuilder
import ndk.ext.shutil
from ndk.hosts import Host
import ndk.packaging
import ndk.paths
from ndk.paths import ANDROID_DIR, NDK_DIR


class ModuleValidateError(RuntimeError):
    """The error raised when module validation fails."""


@unique
class NoticeGroup(Enum):
    """An enum describing NOTICE file groupings.

    The NDK ships two NOTICE files: one for the toolchain, and one for
    everything else.
    """
    BASE = auto()
    TOOLCHAIN = auto()


class BuildContext:
    """Class containing build context information."""

    def __init__(self, out_dir: Path, dist_dir: Path, modules: List[Module],
                 host: Host, arches: List[Arch], build_number: str) -> None:
        self.out_dir = out_dir
        self.dist_dir = dist_dir
        self.modules = {m.name: m for m in modules}
        self.host = host
        self.arches = arches
        self.build_number = build_number


class Module:
    """Base module type for the build system."""

    # pylint wrongly emits no-member if these don't have default values
    # https://github.com/PyCQA/pylint/issues/3167
    #
    # We override __getattribute__ to catch any uses of this value
    # uninitialized and raise an error.
    name: str = ''
    path: Path = Path()
    deps: Set[str] = set()

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if name in ('name', 'path') and attr == '':
            raise RuntimeError(f'Uninitialized use of {name}')
        return attr

    # Used to exclude a module from the build. If explicitly named it will
    # still be built, but it is not included by default.
    enabled = True

    # In most cases a module will have only one license file, so the common
    # interface is a single path, not a list. For the rare modules that have
    # multiple notice files (such as yasm), the notices property should be
    # overrided. By default this property will return `[self.notice]`.
    notice: Optional[Path] = None

    # Not all components need a notice (stub scripts, basic things like the
    # readme and changelog, etc), but this is opt-out.
    no_notice = False

    # Indicates which NOTICE file that should contain the license text for this
    # module. i.e. NoticeGroup.BASE will result in the license being included
    # in $NDK/NOTICE, whereas NoticeGroup.TOOLCHAIN will result in the license
    # text being included in NOTICE.toolchain.
    notice_group = NoticeGroup.BASE

    # If split_build_by_arch is set, one workqueue task will be created for
    # each architecture. The Module object will be cloned for each arch and
    # each will have build_arch set to the architecture that should be built by
    # that module. If build_arch is None, the module has not yet been split.
    split_build_by_arch = False
    build_arch: Optional[Arch] = None

    # Set to True if this module is merely a build convenience and not intented
    # to be shipped. For example, Platforms has its own build steps but is
    # shipped within the Toolchain module. If this value is set, the module's
    # install directory will not be within the NDK.
    intermediate_module = False

    def __init__(self) -> None:
        self.context: Optional[BuildContext] = None
        if self.notice is None:
            self.notice = self.default_notice_path()
        self.validate()

    @property
    def notices(self) -> Iterator[Path]:
        """Iterates over the notice files for this module."""
        if self.no_notice:
            return
        if self.notice is None:
            return
        yield self.notice

    def default_notice_path(self) -> Optional[Path]:
        """Returns the path to the default notice for this module, if any."""
        return None

    def validate_error(self, msg: str) -> ModuleValidateError:
        """Creates a validation error for this module.

        Automatically includes the module name in the error string.

        Args:
            msg: Detailed error message.
        """
        return ModuleValidateError(f'{self.name}: {msg}')

    def validate(self) -> None:
        """Validates module config.

        Raises:
            ModuleValidateError: The module configuration is not valid.
        """
        if self.name is None:
            raise ModuleValidateError(f'{self.__class__} has no name')
        if self.path is None:
            raise self.validate_error('path property not set')
        if self.notice_group not in NoticeGroup:
            raise self.validate_error('invalid notice group')
        self.validate_notice()

    def validate_notice(self) -> None:
        """Validates the notice files of this module.

        Raises:
            ModuleValidateError: The module configuration is not valid.
        """
        if self.no_notice:
            return

        if not self.notices:
            raise self.validate_error('notice property not set')
        for notice in self.notices:
            if not notice.exists():
                raise self.validate_error(
                    f'notice file {notice} does not exist')

    def get_dep(self, name: str) -> Module:
        """Returns the module object for the given dependency.

        Returns:
            The module object for the given dependency.

        Raises:
            KeyError: The given name does not match any of this module's
                dependencies.
        """
        if name not in self.deps:
            raise KeyError
        assert self.context is not None
        return self.context.modules[name]

    def get_build_host_install(self, arch: Optional[Arch] = None) -> Path:
        """Returns the module's install path for the current host.

        In a cross-compiling context (i.e. building the Windows NDK from
        Linux), this will return the install directory for the build OS rather
        than the target OS.

        Args:
            arch: Architecture to fetch for architecture-specific modules.

        Returns:
            This module's install path for the build host.
        """
        return self.get_install_path(Host.current(), arch)

    @property
    def out_dir(self) -> Path:
        """Base out directory for the current build."""
        assert self.context is not None
        return self.context.out_dir

    @property
    def dist_dir(self) -> Path:
        """Base dist directory for the current build."""
        assert self.context is not None
        return self.context.dist_dir

    @property
    def host(self) -> Host:
        """Host for the current build."""
        assert self.context is not None
        return self.context.host

    @property
    def arches(self) -> List[Arch]:
        """Architectures targeted by the current build."""
        assert self.context is not None
        return self.context.arches

    def build(self) -> None:
        """Builds the module.

        A module's dependencies are guaranteed to have been installed before
        its build begins.

        The build phase should not modify the install directory.
        """
        raise NotImplementedError

    def install(self) -> None:
        """Installs the module.

        Install happens after the module has been built.

        The install phase should only copy files, not create them. Compilation
        should happen in the build phase.
        """
        package_installs = ndk.packaging.expand_packages(
            self.name, str(self.path), self.host, self.arches)

        install_base = Path(
            ndk.paths.get_install_path(str(self.out_dir), self.host))
        if self.intermediate_module:
            install_base = self.intermediate_out_dir / 'install'
        for package_name, package_install in package_installs:
            assert self.context is not None
            install_path = install_base / package_install
            package = self.context.dist_dir / package_name
            if install_path.exists():
                shutil.rmtree(install_path)
            ndk.packaging.extract_zip(str(package), str(install_path))

    def get_install_paths(self, host: Host,
                          arches: Optional[Iterable[Arch]]) -> List[Path]:
        """Returns the install paths for the given archiectures."""
        install_subdirs = [
            Path(p)
            for p in ndk.packaging.expand_paths(str(self.path), host, arches)
        ]
        install_base = Path(ndk.paths.get_install_path(str(self.out_dir),
                                                       host))
        if self.intermediate_module:
            install_base = self.intermediate_out_dir / 'install'
        return [install_base / d for d in install_subdirs]

    def get_install_path(self,
                         host: Optional[Host] = None,
                         arch: Optional[Arch] = None) -> Path:
        """Returns the install path for the given module config.

        For an architecture-independent module, there should only ever be one
        install path.

        For an architecture-dependent module, the optional arch argument must
        be provided to select between the install paths.

        Args:
            host: The host to use for a host-specific install path.
            arch: The architecture to use for an architecure-dependent module.

        Raises:
            ValueError: This is an architecture-dependent module and no
                architecture was provided.
            RuntimeError: An architecture-independent module has non-unique
                install paths.
        """
        if host is None:
            host = self.host

        arch_dependent = False
        if ndk.packaging.package_varies_by(str(self.path), 'abi'):
            arch_dependent = True
        elif ndk.packaging.package_varies_by(str(self.path), 'arch'):
            arch_dependent = True
        elif ndk.packaging.package_varies_by(str(self.path), 'toolchain'):
            arch_dependent = True
        elif ndk.packaging.package_varies_by(str(self.path), 'triple'):
            arch_dependent = True

        arches = None
        if arch is not None:
            arches = [arch]
        elif self.build_arch is not None:
            arches = [self.build_arch]
        elif arch_dependent:
            raise ValueError(
                f'get_install_path for {arch} requires valid arch')

        install_subdirs = self.get_install_paths(host, arches)

        if len(install_subdirs) != 1:
            raise RuntimeError(
                f'non-unique install path for single arch: {self.path}')

        return install_subdirs[0]

    @property
    def intermediate_out_dir(self) -> Path:
        """Path for intermediate outputs of this module."""
        base_path = self.out_dir / self.host.value / self.name
        if self.split_build_by_arch:
            return base_path / self.build_arch
        else:
            return base_path

    def __str__(self) -> str:
        if self.split_build_by_arch and self.build_arch is not None:
            return f'{self.name} [{self.build_arch}]'
        return self.name

    def __hash__(self) -> int:
        # The string representation of each module must be unique. This is true
        # both pre- and post-arch split.
        return hash(str(self))

    def __eq__(self, other: object) -> bool:
        # As with hash(), the str must be unique across all modules.
        return str(self) == str(other)

    @property
    def log_file(self) -> str:
        """Returns the basename of the log file for this module."""
        if self.split_build_by_arch and self.build_arch is not None:
            return f'{self.name}-{self.build_arch}.log'
        elif self.split_build_by_arch:
            raise RuntimeError('Called log_file on unsplit module')
        else:
            return f'{self.name}.log'

    def log_path(self, log_dir: Path) -> Path:
        """Returns the path to the log file for this module."""
        return log_dir / self.log_file


class AutoconfModule(Module):
    # Path to the source code
    src: Path
    env: Optional[Dict[str, str]] = None

    _builder: Optional[AutoconfBuilder] = None

    @property
    def builder(self) -> AutoconfBuilder:
        """Returns the lazily initialized builder for this module."""
        if self._builder is None:
            self._builder = AutoconfBuilder(
                self.src / 'configure',
                self.intermediate_out_dir,
                self.host,
                use_clang=True,
                additional_env=self.env)
        return self._builder

    @property
    def configure_args(self) -> List[str]:
        return [
            "--disable-nls",
            "--disable-rpath",
        ]

    def build(self) -> None:
        self.builder.build(self.configure_args)

    def install(self) -> None:
        install_dir = self.get_install_path()
        install_dir.mkdir(parents=True, exist_ok=True)
        copy_tree(
            str(self.builder.install_directory),
            str(install_dir))


class PackageModule(Module):
    """A directory to be installed to the NDK.

    No transformation is performed on the installed directory.
    """

    #: The absolute path to the directory to be installed.
    src: Path

    def default_notice_path(self) -> Path:
        return self.src / 'NOTICE'

    def validate(self) -> None:
        super().validate()

        if ndk.packaging.package_varies_by(str(self.path), 'abi'):
            raise self.validate_error('PackageModule cannot vary by abi')
        if ndk.packaging.package_varies_by(str(self.path), 'arch'):
            raise self.validate_error('PackageModule cannot vary by arch')
        if ndk.packaging.package_varies_by(str(self.path), 'toolchain'):
            raise self.validate_error('PackageModule cannot vary by toolchain')
        if ndk.packaging.package_varies_by(str(self.path), 'triple'):
            raise self.validate_error('PackageModule cannot vary by triple')

    def build(self) -> None:
        pass

    def install(self) -> None:
        install_paths = self.get_install_paths(self.host, ALL_ARCHITECTURES)
        assert len(install_paths) == 1
        install_path = install_paths[0]
        install_directory(self.src, install_path)


class InvokeExternalBuildModule(Module):
    """A module that uses a build.py script.

    These are legacy modules that have not yet been properly merged into
    checkbuild.py.
    """

    #: The path to the build script relative to the top of the source tree.
    script: Path

    #: True if the module can be built in parallel per-architecture.
    arch_specific = False

    def build(self) -> None:
        build_args = common_build_args(self.out_dir, self.dist_dir, self.host)
        if self.split_build_by_arch:
            build_args.append(f'--arch={self.build_arch}')
        elif self.arch_specific and len(self.arches) == 1:
            build_args.append(f'--arch={self.arches[0]}')
        elif set(self.arches) == set(ALL_ARCHITECTURES):
            pass
        else:
            raise NotImplementedError(
                f'Module {self.name} can only build all architectures or none')
        script = self.get_script_path()
        invoke_external_build(script, build_args)

    def get_script_path(self) -> Path:
        """Returns the absolute path to the build script."""
        return ANDROID_DIR / self.script


# TODO: Convert shadertools and remove this.
class InvokeBuildModule(InvokeExternalBuildModule):
    """A module that uses a build.py script within ndk/build/tools.

    Identical to InvokeExternalBuildModule, but the script path is relative to
    ndk/build/tools instead of the top of the source tree.
    """

    def get_script_path(self) -> Path:
        return NDK_DIR / 'build/tools' / self.script


class FileModule(Module):
    """A module that installs a single file to the NDK."""

    #: Path to the file to be installed.
    src: Path

    #: True if no notice file is needed for this module.
    no_notice = True

    def build(self) -> None:
        pass

    def install(self) -> None:
        install_path = self.get_install_path()
        install_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.src, install_path)


class MultiFileModule(Module):
    """A module that installs multiple files to the NDK.

    This is similar to FileModule, but allows multiple files to be installed
    with a single module.
    """

    @property
    def files(self) -> Iterator[Path]:
        """List of absolute paths to files to be installed."""
        yield from []

    def build(self) -> None:
        pass

    def install(self) -> None:
        install_dir = self.get_install_path()
        install_dir.mkdir(parents=True, exist_ok=True)
        for file_path in self.files:
            shutil.copy2(file_path, install_dir)


class ScriptShortcutModule(Module):
    """A module that installs a shortcut to another script in the NDK.

    Some NDK tools are installed to a location other than the top of the NDK
    (as a result of the modular NDK effort), but we want to make them
    accessible from the top level because that is where they were Historically
    installed.
    """

    #: The path to the installed NDK script, relative to the top of the NDK.
    script: Path

    #: The file extension for the called script on Windows.
    windows_ext: str

    # These are all trivial shell scripts that we generated. No notice needed.
    no_notice = True

    def validate(self) -> None:
        super().validate()

        if ndk.packaging.package_varies_by(str(self.script), 'abi'):
            raise self.validate_error(
                'ScriptShortcutModule cannot vary by abi')
        if ndk.packaging.package_varies_by(str(self.script), 'arch'):
            raise self.validate_error(
                'ScriptShortcutModule cannot vary by arch')
        if ndk.packaging.package_varies_by(str(self.script), 'toolchain'):
            raise self.validate_error(
                'ScriptShortcutModule cannot vary by toolchain')
        if ndk.packaging.package_varies_by(str(self.script), 'triple'):
            raise self.validate_error(
                'ScriptShortcutModule cannot vary by triple')
        if self.windows_ext is None:
            raise self.validate_error(
                'ScriptShortcutModule requires windows_ext')

    def build(self) -> None:
        pass

    def install(self) -> None:
        if self.host.is_windows:
            self.make_cmd_helper()
        else:
            self.make_sh_helper()

    def make_cmd_helper(self) -> None:
        """Makes a .cmd helper script for Windows."""
        script = self.get_script_path().with_suffix(self.windows_ext)
        full_path = PureWindowsPath('%~dp0') / script

        install_path = self.get_install_path().with_suffix('.cmd')
        install_path.write_text(
            textwrap.dedent(f"""\
                @echo off
                {full_path} %*
                """))

    def make_sh_helper(self) -> None:
        """Makes a bash helper script for POSIX systems."""
        script = self.get_script_path()
        full_path = Path('$DIR') / script

        install_path = self.get_install_path()
        install_path.write_text(
            textwrap.dedent(f"""\
                #!/bin/sh
                DIR="$(cd "$(dirname "$0")" && pwd)"
                {full_path} "$@"
                """))
        mode = install_path.stat().st_mode
        install_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def get_script_path(self) -> Path:
        """Returns the installed path of the script."""
        scripts = [
            Path(p) for p in ndk.packaging.expand_paths(
                str(self.script), self.host, ALL_ARCHITECTURES)
        ]
        assert len(scripts) == 1
        return scripts[0]


class PythonPackage(Module):
    """A Python package that should be packaged for distribution.

    These are not installed within the NDK, but are packaged (as a source
    distribution) for archival on the build servers.

    These are used to archive the NDK's build and test tools so test artifacts
    may be regnenerated using only artifacts from the build server.
    """

    def default_notice_path(self) -> Path:
        # Assume there's a NOTICE file in the same directory as the setup.py.
        return self.path.parent / 'NOTICE'

    def build(self) -> None:
        subprocess.check_call(
            ['python3', str(self.path), 'sdist', '-d', self.out_dir],
            cwd=self.path.parent)

    def install(self) -> None:
        pass


def invoke_external_build(script: Path, args: List[str]) -> None:
    """Invokes a build.py script rooted within the top level source tree.

    Args:
        script: Path to the script to be executed within the top level source
            tree.
        args: Command line arguments to be passed to the script.
    """
    subprocess.check_call(['python3', str(script)] + args)


def common_build_args(out_dir: Path, dist_dir: Path, host: Host) -> List[str]:
    """Returns a list of common arguments for build.py scripts.

    Modules that have not been fully merged into checkbuild.py still use a
    separately executed build.py script via InvokeBuildModule or
    InvokeExternalBuildModule. These have a common command line interface for
    determining out directories and target host.

    Args:
        out_dir: Base out directory for the target host.
        dist_dir: Distribution directory for archived artifacts.
        host: Target host.

    Returns:
        List of command line arguments to be used with build.py.
    """
    return [
        f'--out-dir={out_dir / host.value}',
        f'--dist-dir={dist_dir}',
        f'--host={host.value}',
    ]


def install_directory(src: Path, dst: Path) -> None:
    """Copies a directory to an install location, ignoring some file types.

    The destination will be removed prior to copying if it exists, ensuring a
    clean install.

    Some file types (currently python intermediates, editor swap files, git
    directories) will be removed from the install location.

    Args:
        src: Directory to install.
        dst: Install location. Will be removed prior to installation. The
            source directory will be copied *to* this path, not *into* this
            path.
    """
    # TODO: Remove the ignore patterns in favor of purging the install
    # directory after install, since not everything uses install_directory.
    #
    # We already do this to some extent with package_ndk, but we don't cover
    # all these file types, and we should also do this before packaging since
    # packaging only runs when a full NDK is built (fine for the build servers,
    # could potentially be wrong for local testing).
    if dst.exists():
        shutil.rmtree(dst)
    ignore_patterns = shutil.ignore_patterns('*.pyc', '*.pyo', '*.swp',
                                             '*.git*')
    shutil.copytree(src, dst, ignore=ignore_patterns)


def make_repo_prop(out_dir: Path) -> None:
    """Installs a repro.prop file to the given directory.

    A repo.prop file is a file listing all of the git projects used and their
    checked out revisions. i.e.

        platform/bionic 40538268d43d82409a93637960f2da3c1226840a
        platform/development 688f15246399db98897e660889d9a202559fe5d8
        ...

    Historically we installed one of these per "module" (from the attempted
    modular NDK), but since the same information can be retrieved from the
    build number we do not install them for most things now.

    If this build is happening on the build server then there will be a
    repo.prop file in the DIST_DIR for us to copy, otherwise we generate our
    own.
    """
    # TODO: Finish removing users of this in favor of installing a single
    # manifest.xml file in the root of the NDK.
    file_name = 'repo.prop'

    dist_dir = os.environ.get('DIST_DIR')
    if dist_dir is not None:
        dist_repo_prop = Path(dist_dir) / file_name
        shutil.copy(dist_repo_prop, out_dir)
    else:
        out_file = out_dir / file_name
        with out_file.open('w') as prop_file:
            cmd = [
                'repo', 'forall', '-c',
                'echo $REPO_PROJECT $(git rev-parse HEAD)',
            ]
            subprocess.check_call(cmd, stdout=prop_file)
