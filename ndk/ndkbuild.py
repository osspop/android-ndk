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
"""APIs for interacting with ndk-build."""
from __future__ import absolute_import

import os
from pathlib import Path
from typing import List, Tuple

import ndk.ext.subprocess


def build(ndk_path: Path, build_flags: List[str]) -> Tuple[int, str]:
    """Invokes ndk-build with the given arguments."""
    ndk_build_path = ndk_path / "ndk-build"
    cmd = [str(ndk_build_path)] + build_flags
    if os.name == "nt":
        cmd = ["cmd", "/c"] + cmd
    return ndk.ext.subprocess.call_output(cmd, encoding="utf-8")
