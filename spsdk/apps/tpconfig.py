#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2021-2022 NXP
#
# SPDX-License-Identifier: BSD-3-Clause

"""Console script for Trust provisioning config application."""
import logging
import os
import sys
from typing import List

import click

from spsdk.apps.tp_utils import (
    TPConfigConfig,
    device_help,
    get_counters,
    list_tpdevices,
    multiple_tp_dict,
    process_tp_inputs,
    tp_device_options,
)
from spsdk.apps.utils import CommandsTreeGroup, catch_spsdk_error, spsdk_apps_common_options
from spsdk.tp import TP_DATA_FOLDER, SPSDKTpError, TpDevInterface, TrustProvisioningConfig
from spsdk.tp.utils import get_supported_devices, scan_tp_devices


@click.group(name="tpconfig", cls=CommandsTreeGroup)
@spsdk_apps_common_options
def main(log_level: int) -> int:
    """Application for configuration of trusted device."""
    logging.basicConfig(level=log_level or logging.WARNING)
    return 0


@main.command(name="load", no_args_is_help=True)
@tp_device_options
@click.option(
    "-t",
    "--timeout",
    type=click.IntRange(0, 600, clamp=True),
    help="The target provisioning timeout in seconds.",
)
@click.option(
    "-c",
    "--config",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (parameters on CLI take precedence).",
    required=True,
)
@click.option(
    "-m",
    "--include-manufacturing",
    is_flag=True,
    default=False,
    help="Include manufacturing data (no longer supported!)",
)
@click.option(
    "-s",
    "--seal",
    is_flag=True,
    default=False,
    help="""
        Seal the smart card (advancing its lifecycle to PRODUCTION).
        WARNING: You'll not be able to use tpconfig afterwards!""",
)
def load(
    tp_device: str,
    tp_device_parameter: List[str],
    timeout: int,
    config: str,
    include_manufacturing: bool,
    seal: bool,
) -> None:
    """Command to load configuration to the TP device."""
    if include_manufacturing:
        TPConfigConfig.SCHEMA_MEMBERS.append("tp_config_manufacturing")
    tp_config = TPConfigConfig(config, tp_device, tp_device_parameter, timeout)

    tp_interface = process_tp_inputs(
        tp_type=tp_config.tp_device,
        tp_parameters=tp_config.tp_device_parameter,
        header="device",
        scan_func=scan_tp_devices,
        print_func=click.echo,
    )
    tp_dev = tp_interface.create_interface()
    assert isinstance(tp_dev, TpDevInterface)

    tp_worker = TrustProvisioningConfig(tp_dev, click.echo)
    if include_manufacturing:
        raise SPSDKTpError(
            "The option --include-manufacturing is no longer supported. "
            "Please use officially pre-personalized cards."
        )
    tp_worker.upload(tp_config.config_data, tp_config.config_dir)
    if seal:
        tp_worker.seal()


@main.command(name="seal", no_args_is_help=True)
@tp_device_options
@click.option(
    "-t",
    "--timeout",
    type=click.IntRange(0, 600, clamp=True),
    help="The target provisioning timeout in seconds.",
)
@click.option(
    "-c",
    "--config",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to configuration file (parameters on CLI take precedence).",
    required=False,
)
def seal(
    tp_device: str,
    tp_device_parameter: List[str],
    timeout: int,
    config: str,
) -> None:
    """Seal the smart card (advancing its lifecycle to PRODUCTION).

    WARNING: You'll not be able to use tpconfig afterwards!
    """
    if config:
        tp_config = TPConfigConfig(config, tp_device, tp_device_parameter, timeout)
        device = tp_config.tp_device
        params = tp_config.tp_device_parameter
    else:
        device = tp_device
        params = multiple_tp_dict(tp_device_parameter)

    if not device:
        raise SPSDKTpError("TP Device's type is not specified ")
    if "id" not in params or not params["id"]:
        raise SPSDKTpError("TP Device's ID is not specified")

    tp_interface = process_tp_inputs(
        tp_type=device,
        tp_parameters=params,
        header="device",
        scan_func=scan_tp_devices,
        print_func=click.echo,
    )
    tp_dev = tp_interface.create_interface()
    assert isinstance(tp_dev, TpDevInterface)

    tp_worker = TrustProvisioningConfig(tp_dev, click.echo)
    tp_worker.seal()


@main.command(name="get-cfg-template", no_args_is_help=True)
@click.option(
    "-f",
    "--family",
    type=click.Choice(get_supported_devices(), case_sensitive=False),
    default="lpc55s6x",
    help="Chip family to generate the TPConfig config for.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    required=True,
    help="The output YAML template configuration file name",
)
def get_cfg_template(family: str, output: str) -> None:
    """Command to generate tphost template of configuration YML file."""
    with open(os.path.join(TP_DATA_FOLDER, "tpconfig_cfg_template.yml"), "r") as file:
        template = file.read()

    with open(str(output), "w") as file:
        file.write(template)

    click.echo(f"The configuration template created. {os.path.abspath(output)}")


main.add_command(device_help)
main.add_command(list_tpdevices)
main.add_command(get_counters)


@catch_spsdk_error
def safe_main() -> None:
    """Call the main function."""
    sys.exit(main())  # pragma: no cover  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    safe_main()  # pragma: no cover