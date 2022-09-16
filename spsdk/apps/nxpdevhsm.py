#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright 2021-2022 NXP
#
# SPDX-License-Identifier: BSD-3-Clause

"""Module is used to generate initialization SB file."""

import logging
import os
import sys
from typing import Any, BinaryIO, Callable, Dict, List, Tuple

import click

from spsdk import SPSDKError, SPSDKValueError
from spsdk import __version__ as version
from spsdk.apps.utils.common_cli_options import (
    CommandsTreeGroup,
    isp_interfaces,
    spsdk_apps_common_options,
)
from spsdk.apps.utils.utils import catch_spsdk_error, format_raw_data, get_interface
from spsdk.image import MBIMG_SCH_FILE, SB3_SCH_FILE
from spsdk.mboot.commands import TrustProvKeyType, TrustProvOemKeyType
from spsdk.mboot.interfaces import MBootInterface
from spsdk.mboot.mcuboot import McuBoot
from spsdk.sbfile.sb31.commands import CmdLoadKeyBlob
from spsdk.sbfile.sb31.images import SecureBinary31Commands, SecureBinary31Header
from spsdk.utils.crypto.cert_blocks import CertificateBlockHeader
from spsdk.utils.crypto.common import crypto_backend
from spsdk.utils.misc import load_configuration, value_to_int
from spsdk.utils.schema_validator import ValidationSchemas, check_config

logger = logging.getLogger(__name__)


class DeviceHsm:
    """Class to handle device HSM provisioning procedure."""

    DEVBUFF_BASE = 0x20008000
    DEVBUFF_MAX_SIZE = 0x8000
    DEVBUFF_SIZE = 0x100

    DEVBUFF_BASE0 = DEVBUFF_BASE
    DEVBUFF_BASE1 = DEVBUFF_BASE0 + DEVBUFF_SIZE
    DEVBUFF_BASE2 = DEVBUFF_BASE1 + DEVBUFF_SIZE
    DEVBUFF_BASE3 = DEVBUFF_BASE2 + DEVBUFF_SIZE

    DEVBUFF_GEN_MASTER_SHARE_INPUT_SIZE = 16
    DEVBUFF_GEN_MASTER_ENC_SHARE_OUTPUT_SIZE = 48
    DEVBUFF_GEN_MASTER_ENC_MASTER_SHARE_OUTPUT_SIZE = 64
    DEVBUFF_GEN_MASTER_CUST_CERT_PUK_OUTPUT_SIZE = 64
    DEVBUFF_HSM_GENKEY_KEYBLOB_SIZE = 48
    DEVBUFF_HSM_GENKEY_KEYBLOB_PUK_SIZE = 64
    DEVBUFF_USER_PCK_KEY_SIZE = 32
    DEVBUFF_WRAPPED_USER_PCK_KEY_SIZE = 48
    DEVBUFF_DATA_BLOCK_SIZE = 256
    DEVBUFF_SB3_SIGNATURE_SIZE = 64

    def __init__(
        self,
        mboot: McuBoot,
        user_pck: bytes,
        oem_share_input: bytes,
        info_print: Callable,
        container_conf: str = None,
        workspace: str = None,
    ) -> None:
        """Initialization of device HSM class. Its design to create provisioned SB3 file.

        :param mboot: mBoot communication interface.
        :param oem_share_input: OEM share input data.
        :param user_pck: USER PCK key.
        :param container_conf: Optional elftosb configuration file (to specify user list of SB commands).
        :param workspace: Optional folder to store middle results.
        :raises SPSDKError: In case of any vulnerability.
        """
        self.mboot = mboot
        self.user_pck = user_pck
        self.oem_share_input = oem_share_input
        self.info_print = info_print
        self.workspace = workspace
        self.container_conf_dir = container_conf
        if self.workspace and not os.path.isdir(self.workspace):
            os.mkdir(self.workspace)

        # store input of OEM_SHARE_INPUT to workspace in case that is generated randomly
        self.store_temp_res("OEM_SHARE_INPUT.BIN", self.oem_share_input)

        # Default value that could be given from SB3 configuration container
        self.timestamp = None
        self.sb3_descr = "SB3 SB_KEK"
        self.sb3_fw_ver = 0

        # Check the configuration file and options to update by user config
        self.config_data = None
        if container_conf:
            config_data = load_configuration(container_conf)
            # validate input configuration
            check_config(
                config_data,
                DeviceHsm.get_validation_schemas(),
                search_paths=[os.path.dirname(container_conf)],
            )
            self.config_data = config_data
            self.sb3_fw_ver = config_data.get("firmwareVersion") or self.sb3_fw_ver
            self.sb3_descr = config_data.get("description") or self.sb3_descr
            if "timestamp" in config_data:
                self.timestamp = value_to_int(str(config_data.get("timestamp")))

        self.wrapped_user_pck = bytes()
        self.final_sb = bytes()

    @classmethod
    def get_validation_schemas(cls) -> List[Dict[str, Any]]:
        """Create the list of validation schemas.

        :return: List of validation schemas.
        """
        mbi_sch_cfg = ValidationSchemas().get_schema_file(MBIMG_SCH_FILE)
        sb3_sch_cfg = ValidationSchemas().get_schema_file(SB3_SCH_FILE)

        ret: List[Dict[str, Any]] = []
        ret.extend([mbi_sch_cfg["firmware_version"]])
        ret.extend([sb3_sch_cfg[x] for x in ["sb3_description", "sb3_commands", "sb3_test"]])
        return ret

    def create_sb3(self) -> None:
        """Do device hsm process to create SB_KEK provisioning SB file."""
        # 1: Call GEN_OEM_MASTER_SHARE to generate encOemShare.bin (ENC_OEM_SHARE will be later put in place of ISK)
        self.info_print(" 1: Generating OEM master share.")
        oem_enc_share, _, _ = self.oem_generate_master_share(self.oem_share_input)

        # 2: Call hsm_gen_key to generate 48 bytes FW signing key
        self.info_print(" 2: Generating 48 bytes FW signing keys.")
        cust_fw_auth_prk, cust_fw_auth_puk = self.generate_key(
            TrustProvOemKeyType.MFWISK, "CUST_FW_AUTH"
        )

        # 3: Call hsm_gen_key to generate 48 bytes FW encryption key
        self.info_print(" 3: Generating 48 bytes FW encryption keys.")
        cust_fw_enc_prk, _ = self.generate_key(TrustProvOemKeyType.MFWENCK, "CUST_FW_ENC_SK")

        # 4: Call hsm_store_key to generate user defined CUST_MK_SK (aka PCK).
        # Will be stored into PFR using loadKeyBlob SB3 command.
        # Use NXP_CUST_KEK_EXT_SK in SB json
        self.info_print(" 4: Wrapping user PCK key.")
        self.wrapped_user_pck = self.wrap_key(self.user_pck)

        # 5: Generate template sb3 fw, sb3ImageType=6
        self.info_print(" 5: Creating template un-encrypted SB3 header and data blobs.")
        # 5.1: Generate SB3.1 header template
        self.info_print(" 5.1: Creating template SB3 header.")
        sb3_header = SecureBinary31Header(
            firmware_version=self.sb3_fw_ver,
            curve_name="secp256r1",
            description=self.sb3_descr,
            timestamp=self.timestamp,
            flags=0x01,  # Bit0: PROV_MFW: when set, the SB3 file encrypts provisioning firmware
        )
        self.timestamp = sb3_header.timestamp
        sb3_header_exported = sb3_header.export()
        logger.debug(
            f" 5.1: The template SB3 header: \n{sb3_header.info()} \n Length:{len(sb3_header_exported)}"
        )

        # 5.2: Create SB3 file un-encrypted data part
        self.info_print(" 5.2: Creating un-encrypted SB3 data.")
        sb3_data = SecureBinary31Commands(
            curve_name="secp256r1", is_encrypted=False, timestamp=self.timestamp
        )
        sb3_data.add_command(
            CmdLoadKeyBlob(
                offset=0x04,
                data=self.wrapped_user_pck,
                key_wrap_id=CmdLoadKeyBlob.KeyWraps["NXP_CUST_KEK_EXT_SK"],
            )
        )
        if self.container_conf_dir:
            sb3_data.load_from_config(
                self.get_cmd_from_config(), search_paths=[self.container_conf_dir]
            )

        logger.debug(f" 5.2: Created un-encrypted SB3 data: \n{sb3_data.info()}")
        # 5.3: Get SB3 file data part individual chunks
        data_cmd_blocks = sb3_data.get_cmd_blocks_to_export()

        # 6: Call hsm_enc_blk to encrypt all the data chunks from step 6. Use FW encryption key from step 3.
        self.info_print(" 6: Encrypting SB3 data on device")
        sb3_enc_data = self.encrypt_data_blocks(
            cust_fw_enc_prk, sb3_header_exported, data_cmd_blocks
        )
        # 6.1: Add to encrypted data parts SHA256 hashes
        self.info_print(" 6.1: Enriching encrypted SB3 data by mandatory hashes.")
        enc_final_data = sb3_data.process_cmd_blocks_to_export(sb3_enc_data)
        self.store_temp_res("Final_data.bin", enc_final_data, "to_merge")

        # 6.2: Create dummy certification part of SB3 manifest
        self.info_print(" 6.2: Creating dummy certificate block.")
        cb_header = CertificateBlockHeader()
        cb_header.cert_block_size = (
            cb_header.SIZE + 68 + self.DEVBUFF_GEN_MASTER_ENC_SHARE_OUTPUT_SIZE
        )
        logger.debug(f" 6.2: The dummy certificate block has been created:\n{cb_header.info()}.")

        # 6.3: Update the SB3 pre-prepared header by current data
        self.info_print(" 6.3: Updating SB3 header by valid values.")
        sb3_header.block_count = sb3_data.block_count
        sb3_header.image_total_length += (
            len(sb3_data.final_hash) + cb_header.cert_block_size + self.DEVBUFF_SB3_SIGNATURE_SIZE
        )
        logger.debug(
            f" 6.3: The SB3 header has been updated by valid values:\n{sb3_header.info()}."
        )

        # 6.4: Compose manifest that will be signed
        self.info_print(" 6.4: Preparing SB3 manifest to sign.")
        manifest_to_sign = bytes()
        manifest_to_sign += sb3_header.export()
        manifest_to_sign += sb3_data.final_hash
        manifest_to_sign += cb_header.export()
        manifest_to_sign += (
            b"\x11\x00\x00\x80"  # 0x80000011  Cert Flags: CA Flag, 1 certificate and NIST P-256
        )
        manifest_to_sign += cust_fw_auth_puk
        manifest_to_sign += oem_enc_share
        self.store_temp_res("manifest_to_sign.bin", manifest_to_sign, "to_merge")
        logger.debug(
            f" 6.4: The SB3 manifest data to sign:\n{format_raw_data(manifest_to_sign, use_hexdump=True)}."
        )

        # 7: Get sign of SB3 file manifest
        self.info_print(" 7: Creating SB3 manifest signature on device.")
        manifest_signature = self.sign_data_blob(manifest_to_sign, cust_fw_auth_prk)
        logger.debug(
            f" 7: The SB3 manifest signature data:\n{format_raw_data(manifest_signature, use_hexdump=True)}."
        )

        # 8: Merge all parts together
        self.info_print(" 8: Composing final SB3 file.")
        self.final_sb = bytes()
        self.final_sb += manifest_to_sign
        self.final_sb += manifest_signature
        self.final_sb += enc_final_data
        self.store_temp_res("Final_SB3.sb3", self.final_sb)
        logger.debug(
            f" 8: The final SB3 file data:\n{format_raw_data(self.final_sb, use_hexdump=True)}."
        )

    def export(self) -> bytes:
        """Get the Final SB file.

        :return: Final SB file in bytes.
        """
        return self.final_sb

    def oem_generate_master_share(self, oem_share_input: bytes) -> Tuple[bytes, bytes, bytes]:
        """Generate on device Encrypted OEM master share outputs.

        :param oem_share_input: OEM input (randomize seed)
        :raises SPSDKError: In case of any vulnerability.
        :return: Tuple with OEM generate master share outputs.
        """
        if not self.mboot.write_memory(self.DEVBUFF_BASE0, oem_share_input):
            raise SPSDKError("Cannot write OEM SHARE INPUT into device.")

        oem_gen_master_share_res = self.mboot.tp_oem_gen_master_share(
            self.DEVBUFF_BASE0,
            self.DEVBUFF_GEN_MASTER_SHARE_INPUT_SIZE,
            self.DEVBUFF_BASE1,
            self.DEVBUFF_SIZE,
            self.DEVBUFF_BASE2,
            self.DEVBUFF_SIZE,
            self.DEVBUFF_BASE3,
            self.DEVBUFF_SIZE,
        )

        if not oem_gen_master_share_res:
            raise SPSDKError(
                "OEM generate master share command failed,"
                " device probably needs reset due to doubled call of this command."
            )

        if (
            oem_gen_master_share_res[0] != self.DEVBUFF_GEN_MASTER_ENC_SHARE_OUTPUT_SIZE
            and oem_gen_master_share_res[1] != self.DEVBUFF_GEN_MASTER_ENC_MASTER_SHARE_OUTPUT_SIZE
            and oem_gen_master_share_res[2] != self.DEVBUFF_GEN_MASTER_CUST_CERT_PUK_OUTPUT_SIZE
        ):
            raise SPSDKError("OEM generate master share command has invalid results.")

        oem_enc_share = self.mboot.read_memory(
            self.DEVBUFF_BASE1,
            self.DEVBUFF_GEN_MASTER_ENC_SHARE_OUTPUT_SIZE,
        )
        if not oem_enc_share:
            raise SPSDKError("Cannot read OEM ENCRYPTED SHARE OUTPUT from device.")
        self.store_temp_res("ENC_OEM_SHARE.bin", oem_enc_share)

        oem_enc_master_share = self.mboot.read_memory(
            self.DEVBUFF_BASE2,
            self.DEVBUFF_GEN_MASTER_ENC_MASTER_SHARE_OUTPUT_SIZE,
        )
        if not oem_enc_master_share:
            raise SPSDKError("Cannot read OEM ENCRYPTED MASTER SHARE OUTPUT from device.")
        self.store_temp_res("ENC_OEM_MASTER_SHARE.bin", oem_enc_master_share)

        oem_cert = self.mboot.read_memory(
            self.DEVBUFF_BASE3,
            self.DEVBUFF_GEN_MASTER_CUST_CERT_PUK_OUTPUT_SIZE,
        )
        if not oem_cert:
            raise SPSDKError("Cannot read OEM CERTIFICATE from device.")
        self.store_temp_res("OEM_CERT.bin", oem_cert)

        return oem_enc_share, oem_enc_master_share, oem_cert

    def generate_key(
        self, key_type: TrustProvOemKeyType, key_name: str = None
    ) -> Tuple[bytes, bytes]:
        """Generate on device key pairs of provided type.

        :param key_type: Type of generated key pairs.
        :param key_name: optional name for storing temporary files.
        :raises SPSDKError: In case of any vulnerability.
        :return: Tuple with Private and Public key.
        """
        hsm_gen_key_res = self.mboot.tp_hsm_gen_key(
            key_type,
            0,
            self.DEVBUFF_BASE0,
            self.DEVBUFF_SIZE,
            self.DEVBUFF_BASE1,
            self.DEVBUFF_SIZE,
        )

        if not hsm_gen_key_res:
            raise SPSDKError("HSM generate key command failed.")

        if (
            hsm_gen_key_res[0] != self.DEVBUFF_HSM_GENKEY_KEYBLOB_SIZE
            and hsm_gen_key_res[1] != self.DEVBUFF_HSM_GENKEY_KEYBLOB_PUK_SIZE
        ):
            raise SPSDKError("OEM generate master share command has invalid results.")

        prk = self.mboot.read_memory(
            self.DEVBUFF_BASE0,
            self.DEVBUFF_HSM_GENKEY_KEYBLOB_SIZE,
        )
        if not prk:
            raise SPSDKError("Cannot read generated private key from device.")

        puk = self.mboot.read_memory(
            self.DEVBUFF_BASE1,
            self.DEVBUFF_HSM_GENKEY_KEYBLOB_PUK_SIZE,
        )
        if not puk:
            raise SPSDKError("Cannot read generated public key from device.")

        self.store_temp_res((key_name or str(TrustProvOemKeyType[str(key_type)])) + "_PRK.bin", prk)
        self.store_temp_res((key_name or str(TrustProvOemKeyType[str(key_type)])) + "_PUK.bin", puk)

        return prk, puk

    def wrap_key(self, user_pck: bytes) -> bytes:
        """Wrap the user PCK key.

        :param user_pck: User PCK key
        :raises SPSDKError: In case of any vulnerability.
        :return: Wrapped user PCK by RFC3396.
        """
        if not self.mboot.write_memory(self.DEVBUFF_BASE0, user_pck):
            raise SPSDKError("Cannot write USER_PCK into device.")

        hsm_store_key_res = self.mboot.tp_hsm_store_key(
            TrustProvKeyType.CKDFK,
            0x01,
            self.DEVBUFF_BASE0,
            self.DEVBUFF_USER_PCK_KEY_SIZE,
            self.DEVBUFF_BASE1,
            self.DEVBUFF_SIZE,
        )

        if not hsm_store_key_res:
            raise SPSDKError("HSM Store Key command failed.")

        if hsm_store_key_res[1] != self.DEVBUFF_WRAPPED_USER_PCK_KEY_SIZE:
            raise SPSDKError("HSM Store Key command has invalid results.")

        wrapped_user_pck = self.mboot.read_memory(
            self.DEVBUFF_BASE1,
            self.DEVBUFF_WRAPPED_USER_PCK_KEY_SIZE,
        )
        if not wrapped_user_pck:
            raise SPSDKError("Cannot read WRAPPED USER PCK from device.")

        self.store_temp_res("CUST_MK_SK.bin", wrapped_user_pck)

        return wrapped_user_pck

    def sign_data_blob(self, data_to_sign: bytes, key: bytes) -> bytes:
        """Get HSM encryption sign for data blob.

        :param data_to_sign: Input data to sign.
        :param key: FW signing key (MFWISK).
        :raises SPSDKError: In case of any vulnerability.
        :return: Data blob signature (64 bytes).
        """
        if not self.mboot.write_memory(self.DEVBUFF_BASE0, key):
            raise SPSDKError("Cannot write signing key into device.")
        if not self.mboot.write_memory(self.DEVBUFF_BASE1, data_to_sign):
            raise SPSDKError("Cannot write Data to sign into device.")
        hsm_gen_key_res = self.mboot.tp_hsm_enc_sign(
            self.DEVBUFF_BASE0,
            len(key),
            self.DEVBUFF_BASE1,
            len(data_to_sign),
            self.DEVBUFF_BASE2,
            self.DEVBUFF_SB3_SIGNATURE_SIZE,
        )

        if hsm_gen_key_res != self.DEVBUFF_SB3_SIGNATURE_SIZE:
            raise SPSDKError("HSM signing command failed.")

        signature = self.mboot.read_memory(
            self.DEVBUFF_BASE2,
            self.DEVBUFF_SB3_SIGNATURE_SIZE,
        )
        if not signature:
            raise SPSDKError("Cannot read generated signature from device.")

        self.store_temp_res("SB3_sign.bin", signature, "to_merge")

        return signature

    def store_temp_res(self, file_name: str, data: bytes, group: str = None) -> None:
        """Storing temporary files into workspace.

        :param file_name: Name of file to store the data.
        :param data: Data to store.
        :param group: Subfolder name, defaults to None
        """
        if not self.workspace:
            return
        group_dir = os.path.join(self.workspace, group or "")
        if not os.path.isdir(group_dir):
            os.mkdir(group_dir)

        filename = os.path.join(self.workspace, group or "", file_name)

        with open(filename, "wb") as data_file:
            data_file.write(data)

    def get_cmd_from_config(self) -> List[Dict[str, Any]]:
        """Process command description into a command object.

        :return: Modified list of commands
        :raises SPSDKError: Unknown command
        """
        cfg_commands: List[Dict[str, Any]] = []
        if self.config_data and self.config_data.get("commands"):
            cfg_commands = self.config_data["commands"]
            for cmd in cfg_commands:
                cmd_cpy: Dict = cmd.copy()
                name, args = cmd_cpy.popitem()
                if name == "loadKeyBlob" and value_to_int(str(args["offset"])) == 0x04:
                    raise SPSDKError(
                        f"""The duplicated 'loadKeyBlob' on offset 0x04 from
                    configuration file:\n {args}."""
                    )

        return cfg_commands

    def encrypt_data_blocks(
        self, cust_fw_enc_key: bytes, sb3_header: bytes, data_cmd_blocks: List[bytes]
    ) -> List[bytes]:
        """Encrypt all data blocks on device.

        :param cust_fw_enc_key: Firmware encryption key.
        :param sb3_header: Un Encrypted SB3 file header.
        :param data_cmd_blocks: List of un-encrypted SB3 file command blocks.
        :raises SPSDKError: In case of any vulnerability.
        :return: List of encrypted command blocks on device.
        """
        if not self.mboot.write_memory(self.DEVBUFF_BASE0, cust_fw_enc_key):
            raise SPSDKError("Cannot write customer fw encryption key into device.")
        self.store_temp_res("SB3_header.bin", sb3_header, "to_encrypt")
        if not self.mboot.write_memory(self.DEVBUFF_BASE1, sb3_header):
            raise SPSDKError("Cannot write SB3 header into device.")

        encrypted_blocks = []
        for data_cmd_block_ix, data_cmd_block in enumerate(data_cmd_blocks, start=1):
            self.store_temp_res(f"SB3_block_{data_cmd_block_ix}.bin", data_cmd_block, "to_encrypt")
            if not self.mboot.write_memory(self.DEVBUFF_BASE2, data_cmd_block):
                raise SPSDKError(f"Cannot write SB3 data block{data_cmd_block_ix} into device.")

            if not self.mboot.tp_hsm_enc_blk(
                self.DEVBUFF_BASE0,
                len(cust_fw_enc_key),
                16,
                self.DEVBUFF_BASE1,
                len(sb3_header),
                data_cmd_block_ix,
                self.DEVBUFF_BASE2,
                self.DEVBUFF_DATA_BLOCK_SIZE,
            ):
                raise SPSDKError(
                    f"Cannot run SB3 data block_{data_cmd_block_ix} HSM Encryption in device."
                )

            encrypted_block = self.mboot.read_memory(
                self.DEVBUFF_BASE2,
                self.DEVBUFF_DATA_BLOCK_SIZE,
            )
            if not encrypted_block:
                raise SPSDKError(f"Cannot read SB3 data block_{data_cmd_block_ix} from device.")

            self.store_temp_res(f"SB3_block_{data_cmd_block_ix}.bin", encrypted_block, "encrypted")

            encrypted_blocks.append(encrypted_block)

        return encrypted_blocks


def get_user_pck(key: BinaryIO) -> bytes:
    """Get binary from text or binary file.

    :param key: Binary user PCK key file.
    :return: Binary array loaded from file.
    :raises SPSDKValueError: When invalid input value is recognized.
    """
    user_pck = key.read()

    if len(user_pck) != 32:
        raise SPSDKValueError(
            f"Invalid length of USER PCK INPUT ({len(user_pck)} not equal to 32)."
        )

    return user_pck


def get_oem_share_input(binary: BinaryIO) -> bytes:
    """Get binary from text or binary file.

    :param binary: Path to binary file.
    :return: Binary array loaded from file.
    :raises SPSDKValueError: When invalid input value is recognized.
    """
    if binary:
        oem_share_input = binary.read()
    else:
        oem_share_input = crypto_backend().random_bytes(16)

    if len(oem_share_input) != 16:
        raise SPSDKValueError(
            f"Invalid length of OEM SHARE INPUT ({len(oem_share_input)} not equal to 16)."
        )

    return oem_share_input


@click.group(name="nxpdevhsm", no_args_is_help=True, cls=CommandsTreeGroup)
@spsdk_apps_common_options
def main(log_level: int) -> int:
    """Nxpdevhsm application is designed to create SB3 provisioning file for initial provisioning of device by OEM."""
    logging.basicConfig(level=log_level or logging.WARNING)
    return 0


@main.command(no_args_is_help=True)
@isp_interfaces(uart=True, usb=True, lpcusbsio=True, buspal=True, json_option=False)
@click.option(
    "-k",
    "--key",
    type=click.File(mode="rb"),
    required=True,
    help="User PCK secret file (32-bytes long binary file). PCK (provisioned by OEM, known by OEM) - Part Common Key."
    " This is a 256-bit pre-shared AES key provisioned by OEM. PCK is used to derive FW image encryption keys.",
)
@click.option(
    "-o",
    "--oem-share-input",
    type=click.File(mode="rb"),
    help="OEM share input file to use as a seed to randomize the provisioning process (16-bytes long binary file).",
)
@click.option(
    "-w",
    "--workspace",
    type=click.Path(),
    required=False,
    help="Workspace folder to store temporary files, that could be used for future review.",
)
@click.option(
    "-j",
    "--container-conf",
    type=click.Path(exists=True),
    required=False,
    help="""json container configuration file to produce secure binary v3.x.
    In this configuration file is enough to provide just commands and description section.""",
)
@click.argument("output-path", type=click.File(mode="wb"))
def generate(
    port: str,
    usb: str,
    buspal: str,
    lpcusbsio: str,
    oem_share_input: BinaryIO,
    key: BinaryIO,
    output_path: BinaryIO,
    workspace: click.Path,
    container_conf: click.Path,
    timeout: int,
) -> None:
    """Generate provisioning SB3.1 file.

    \b
    PATH    - output file path, where the final provisioned SB file will be stored.
    """
    interface = get_interface(
        module="mboot", port=port, usb=usb, lpcusbsio=lpcusbsio, timeout=timeout, buspal=buspal
    )
    assert isinstance(interface, MBootInterface)

    oem_share_in = get_oem_share_input(oem_share_input)
    user_pck = get_user_pck(key)

    with McuBoot(interface) as mboot:
        devhsm = DeviceHsm(
            mboot=mboot,
            user_pck=user_pck,
            oem_share_input=oem_share_in,
            info_print=click.echo,
            container_conf=str(container_conf) if container_conf else None,
            workspace=str(workspace) if workspace else None,
        )

        devhsm.create_sb3()
        output_path.write(devhsm.export())

    click.echo(f"Final SB3 file has been written: {os.path.abspath(output_path.name)}")


@catch_spsdk_error
def safe_main() -> None:
    """Call the main function."""
    sys.exit(main())  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    safe_main()