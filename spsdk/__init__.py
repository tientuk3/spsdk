#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2019-2024 NXP
#
# SPDX-License-Identifier: BSD-3-Clause

"""Secure Provisioning SDK is unified, reliable and easy to use SW library.

It's working across NXP MCU portfolio providing strong foundation from quick customer
prototyping up to production deployment.
The library allows the user to
    - connect and communicate with a device
    - configure the device; prepare
    - download and upload data
    - providing security operations.
It is delivered in a form of python library with functionality presented as CLI or GUI utilities.
"""
import os

from .__version__ import __version__ as version

__author__ = "NXP"
__contact__ = "michal.starecek@nxp.com"
__license__ = "BSD-3-Clause"
__version__ = version
__release__ = "beta"

# The SPSDK behavior settings
# SPSDK_DATA_FOLDER might be redefined by SPSDK_DATA_FOLDER_{version}
# or SPSDK_DATA_FOLDER env variable
SPSDK_DATA_FOLDER_ENV_VERSION = "SPSDK_DATA_FOLDER_" + version.replace(".", "_")
SPSDK_DATA_FOLDER = (
    os.environ.get(SPSDK_DATA_FOLDER_ENV_VERSION)
    or os.environ.get("SPSDK_DATA_FOLDER")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
SPSDK_DATA_FOLDER_COMMON = os.path.join(SPSDK_DATA_FOLDER, "common")
SPSDK_DATA_FOLDER_SCHEMAS = os.path.join(SPSDK_DATA_FOLDER, "jsonschemas")

# SPSDK_CACHE_DISABLED might be redefined by SPSDK_CACHE_DISABLED_{version} env variable, default is False
SPSDK_ENV_CACHE_DISABLED = "SPSDK_CACHE_DISABLED_" + version.replace(".", "_")
SPSDK_CACHE_DISABLED = bool(
    os.environ.get(SPSDK_ENV_CACHE_DISABLED) or os.environ.get("SPSDK_CACHE_DISABLED") or False
)

SPSDK_YML_INDENT = 2


ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SPSDK_EXAMPLES_FOLDER = os.path.relpath(os.path.join(ROOT_DIR, "examples"))
