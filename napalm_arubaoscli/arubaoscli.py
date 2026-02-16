# -*- coding: utf-8 -*-
# Copyright 2024 Vanderlay Technology Ltd. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

"""
Napalm driver for ArubaOSCLI.

Read https://napalm.readthedocs.io for more information.
"""

import tempfile
import ipaddress
import difflib
from threading import Thread
import socket
import time
import io
import re
import logging
import os

from napalm.base import NetworkDriver
from napalm.base.exceptions import (
    ConnectionException,
    SessionLockedException,
    MergeConfigException,
    ReplaceConfigException,
    CommandErrorException,
)

from netmiko import ConnectHandler
import tftpy

logger = logging.getLogger(__name__)
logging.getLogger("tftpy.TftpServer").setLevel(logging.ERROR)


class ArubaOSCLIDriver(NetworkDriver):
    """Napalm driver for ArubaOSCLI."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        """Constructor."""
        self.device = None
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.port = 22

        # by default, 6300 series switches should be inside the MGMT VRF.
        # This is required for tftp copy command to ensure that the correct route is in place
        if "6300" in hostname:
            self.mgmt_vrf = "MGMT"
        else:
            self.mgmt_vrf = "default"

        if optional_args is None:
            optional_args = {}
        self.optional_args = optional_args

        self.merge_candidate = False
        self.replace_candidate = False

    def open(self):
        """Implement the NAPALM method open (mandatory)"""
        device = {
            "device_type": "aruba_os",
            "ip": self.hostname,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "timeout": self.timeout,
            "conn_timeout": self.timeout,
            "verbose": False,
        }
        device.update(self.optional_args)

        try:
            self.device = ConnectHandler(**device)
            self.device.session_preparation()
            self.device.send_command("", expect_string=r"#")
            self.device.send_command("no page", expect_string=r"#")

        except Exception:
            raise ConnectionException(
                "Cannot connect to switch via SSH: %s" % (self.hostname)
            )

    def close(self):
        """Implement the NAPALM method close (mandatory)"""
        self.device.disconnect()

    def send_command(self, command_list, expect_string=r"#"):
        """Convenience function for self.device.send_command
        Supports a single command, or a list of commands
        """
        if type(command_list) == str:
            return self.device.send_command(command_list, expect_string=expect_string)

        return self.device.send_multiline(command_list, expect_string=expect_string)

    def get_config(self, retrieve="all", full=False):
        """
        Return the configuration of a device. Currently this is limited to JSON format

        :param retrieve: String to determine which configuration type you want to retrieve, default is all of them.
                              The rest will be set to "".
        :param full: Boolean to retrieve all the configuration. (Not supported)
        :return: The object returned is a dictionary with a key for each configuration store:
            - running(string) - Representation of the native running configuration
            - candidate(string) - Representation of the candidate configuration (not supported on aruba os)
            - startup(string) - Representation of the native startup configuration.
        """
        if retrieve not in ["running", "candidate", "startup", "all"]:
            raise Exception(
                "ERROR: Not a valid option to retrieve.\nPlease select from 'running', 'candidate', "
                "'startup', or 'all'"
            )
        else:
            config_dict = {"running": "", "startup": "", "candidate": ""}
            if retrieve in ["running", "all"]:
                config_dict["running"] = self.send_command("show running-config")
            if retrieve in ["startup", "all"]:
                config_dict["startup"] = self.send_command("show startup-config")

        return config_dict

    def is_alive(self):
        try:
            self.send_command("")
            return {"is_alive": True}
        except AttributeError:
            return {"is_alive": False}

    def compare_config(self):
        raise NotImplementedError("Config compare not supported on merge configs")

    def discard_config(self):
        self.merge_candidate = False
        self.replace_candidate = False

    def load_merge_candidate(self, filename=None, config=None):
        if filename and config:
            raise MergeConfigException("Cannot specify both filename and config")

        if filename:
            with open(filename, "r") as stream:
                self.merge_candidate = stream.read()

        if config:
            self.merge_candidate = config

    def load_replace_candidate(self, filename=None, config=None):
        if filename and config:
            raise ReplaceConfigException("Cannot specify both filename and config")

        if filename:
            with open(filename, "r") as stream:
                self.replace_candidate = stream.read()

        if config:
            self.replace_candidate = config

    def commit_config(self, message=""):
        """
        Send self.merge_candidate to running-config via tftp
        """

        if self.merge_candidate and self.replace_candidate:
            raise MergeConfigException("Both merge and replace candidate found")

        if not self.merge_candidate and not self.replace_candidate:
            raise MergeConfigException("No candidate loaded")

        if self.merge_candidate:
            result = self._transfer_file(self.merge_candidate)
        elif self.replace_candidate:
            result = self._transfer_file(self.replace_candidate)

        if "Some of the configuration lines from the file were NOT applied" in result:
            raise MergeConfigException(
                "Some of the configuration lines from the file were NOT applied."
            )

    def _transfer_file(self, filecontent, destfile="candidate"):
        # Transfer merge candidate with tftp
        with tempfile.TemporaryDirectory() as temp_dir:
            # Setup TFTP server
            tftp_server = tftpy.TftpServer(
                tftproot=temp_dir,
                dyn_file_func=self._tftp_handler(filecontent),
                flock=False #disable file locking
            )
            tftp_thread = Thread(target=tftp_server.listen)
            tftp_thread.daemon = True
            tftp_thread.start()


            result = self.send_command(
                [f"copy tftp://{self._get_ipaddress()}/{destfile} running-config vrf {self.mgmt_vrf}"]
            )

            # Server downloads in the background. Sleep to wait for it
            time.sleep(5)

            tftp_server.stop()
            tftp_thread.join()

            return result

    def _tftp_handler(self, candidate):
        """tftp handler. return candidate no matter what is requested."""

        def _handler(fn, raddress=None, rport=None):
            if fn == "candidate":
                # Use BytesIO instead of StringIO (TFTP transfers bytes)
                # And add a fake 'name' attribute to avoid the lockfile bug
                file_obj = io.BytesIO(candidate.encode('utf-8') if isinstance(candidate, str) else candidate)
                file_obj.name = fn  # Add the name attribute that tftpy expects
                return file_obj
        return _handler

    def _get_ipaddress(self):
        # Use TFTP_SERVER_IP env var if set, otherwise auto-detect
        if os.environ.get("TFTP_SERVER_IP"):
            return os.environ.get("TFTP_SERVER_IP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
