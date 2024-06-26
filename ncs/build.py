#
# Copyright (c) 2023 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
#
"""Implementation of build template engine to link the NCS build system with the suit-generator."""

import os
import sys
import pickle
import pathlib
import re
import logging
import yaml

from jinja2 import Template
from argparse import ArgumentParser

sys.path.insert(0, str(pathlib.Path(__file__).parents[1].absolute()))

from suit_generator.cmd_image import ImageCreator  # noqa: E402

TEMPLATE_CMD = "template"
STORAGE_CMD = "storage"
UPDATE_CMD = "update"

dir_path = pathlib.Path(__file__).parent.absolute()


class BuildConfiguration(dict):
    """Represents a build system configuration, providing access to KConfig values.

    This class reads configuration data from a specified file and parses it.
    Configuration data is accessible as a dictionary.
    """

    config_value_pattern = re.compile(r"(?P<kconfig_name>[A-Za-z0-9_]+)=(?P<kconfig_value>.*)")

    def __init__(self, input_file: str = ".config") -> None:
        """Initialize a BuildConfiguration object."""
        super().__init__()
        try:
            with open(input_file, "r") as fh:
                self._config_data = fh.readlines()
        except FileNotFoundError as e:
            raise SystemExit(e)
        self._parse()

    def _parse(self) -> None:
        """Parse input .config file and populate the configuration dictionary."""
        for config_line in self._config_data:
            if re_result := self.config_value_pattern.match(config_line):
                kconfig_name = re_result.group("kconfig_name")
                kconfig_value = re_result.group("kconfig_value")
                if kconfig_value == "y":
                    # boolean value
                    kconfig_value = True
                elif kconfig_value.startswith("0x"):
                    # hexadecimal value
                    kconfig_value = int(kconfig_value, base=16)
                elif kconfig_value.startswith('"') and kconfig_value.endswith('"'):
                    # string value
                    kconfig_value = kconfig_value[1:-1]
                elif kconfig_value.isdecimal():
                    # int value
                    kconfig_value = int(kconfig_value, base=10)
                super().__setitem__(kconfig_name, kconfig_value)


def read_configurations(configurations):
    """Read configuration stored in the pickled devicetree."""
    data = {}
    for config in configurations:
        name, binary, edt = config.split(",")
        kconfig = pathlib.Path(edt).parent / ".config"
        with open(edt, "rb") as edt_handler:
            edt = pickle.load(edt_handler)
            # add prefix _ to the names starting with digits, for example:
            #   802154_rpmsg_subimage will be available in the templates as _802154_rpmsg_subimage
            data[f"_{name}" if re.match("^[0-9].*]", name) else name] = {
                "name": name,
                "config": BuildConfiguration(kconfig),
                "dt": edt,
                "binary": binary,
            }
    data["get_absolute_address"] = get_absolute_address
    return data


def render_template(template_location, data):
    """Render template using passed data."""
    with open(template_location) as template_file:
        template = Template(template_file.read())
    return template.render(data)


def get_absolute_address(node, use_offset: bool = True):
    """Get absolute address of passed node."""
    # fixme: hardcoded value for parent node due to bug in DTS
    # return node.parent.parent.regs[0].addr + node.regs[0].addr
    if use_offset:
        return 0xE000000 + node.regs[0].addr
    return node.regs[0].addr


if __name__ == "__main__":
    with open(dir_path.parent / "suit_generator" / "logging.yaml", "r") as stream:
        config = yaml.load(stream, Loader=yaml.FullLoader)

    logging.config.dictConfig(config)

    logger = logging.getLogger("ncs")
    logger.debug("ncs build script initialized and logging configuration loaded")

    parent_parser = ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--core",
        action="append",
        required=True,
        help="Configuration of sample name:location of binaries:location of edt",
    )
    parent_parser.add_argument("--zephyr-base", required=True, help="Location of zephyr directory.")

    parser = ArgumentParser(add_help=False)

    subparsers = parser.add_subparsers(dest="command", required=True, help="Choose subcommand:")
    cmd_template_arg_parser = subparsers.add_parser(
        TEMPLATE_CMD, help="Generate SUIT configuration files based on input templates.", parents=[parent_parser]
    )
    cmd_storage_arg_parser = subparsers.add_parser(
        STORAGE_CMD, help="Generate SUIT storage required by secure domain.", parents=[parent_parser]
    )

    cmd_template_arg_parser.add_argument("--artifacts-folder", required=True, help="Output artifact folder.")
    cmd_template_arg_parser.add_argument("--template-suit", required=True, help="Input SUIT jinja2 template.")
    cmd_template_arg_parser.add_argument("--output-suit", required=True, help="Output SUIT configuration.")

    cmd_storage_arg_parser.add_argument(
        "--input-envelope", required=True, action="append", help="Location of input envelope(s)."
    )
    cmd_storage_arg_parser.add_argument(
        "--storage-output-directory", required=True, help="Directory path to store hex files with SUIT storage contents"
    )

    cmd_update_arg_parser = subparsers.add_parser(
        UPDATE_CMD, help="Generate files needed for Secure Domain update", parents=[parent_parser]
    )

    cmd_update_arg_parser.add_argument("--input-file", required=True, help="SUIT envelope in binary format")
    cmd_update_arg_parser.add_argument(
        "--storage-output-file", required=True, help="SUIT storage output file in HEX format"
    )
    cmd_update_arg_parser.add_argument(
        "--dfu-partition-output-file", required=True, help="DFU partition output file in HEX format"
    )
    cmd_update_arg_parser.add_argument(
        "--dfu-partition-address", required=True, type=lambda x: int(x, 0), help="Start address of DFU partition"
    )

    arguments = parser.parse_args()

    logger.debug(f"Received arguments: {arguments}")

    sys.path.insert(0, os.path.join(arguments.zephyr_base, "scripts", "dts", "python-devicetree", "src"))

    configuration = read_configurations(arguments.core)

    if arguments.command == TEMPLATE_CMD:
        configuration["output_envelope"] = arguments.output_suit
        configuration["artifacts_folder"] = arguments.artifacts_folder
        output_suit_content = render_template(arguments.template_suit, configuration)
        with open(arguments.output_suit, "w") as output_file:
            output_file.write(output_suit_content)

    elif arguments.command == STORAGE_CMD:
        # fixme: envelope_address, update_candidate_info_address and dfu_max_caches shall be extracted from DTS
        ImageCreator.create_files_for_boot(
            input_files=arguments.input_envelope,
            storage_output_directory=arguments.storage_output_directory,
            envelope_address=ImageCreator.default_envelope_address,
            envelope_slot_size=ImageCreator.default_envelope_slot_size,
            envelope_slot_count=ImageCreator.default_envelope_slot_count,
            update_candidate_info_address=ImageCreator.default_update_candidate_info_address,
            dfu_max_caches=ImageCreator.default_dfu_max_caches,
        )
    elif arguments.command == UPDATE_CMD:
        ImageCreator.create_files_for_update(
            input_file=arguments.input_file,
            storage_output_file=arguments.storage_output_file,
            dfu_partition_output_file=arguments.dfu_partition_output_file,
            update_candidate_info_address=ImageCreator.default_update_candidate_info_address,
            dfu_partition_address=arguments.dfu_partition_address,
            dfu_max_caches=ImageCreator.default_dfu_max_caches,
        )
