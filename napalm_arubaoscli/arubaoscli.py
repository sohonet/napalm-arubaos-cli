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
from threading import Thread, Event
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


class _CandidateFile:
    """Candidate config served over TFTP, flagging when it has been read out.

    tftpy pulls the file in fixed-size blocks, so a read shorter than the
    requested block size means the final block has been handed over and the
    switch now has the whole candidate. That is a real completion signal to
    wait on instead of guessing with a fixed sleep.

    This wraps a genuine file object rather than subclassing StringIO because
    tftpy >= 0.8.5 advisory-locks whatever dyn_file_func returns, which needs
    both a .name and a real file descriptor. Everything except read/close is
    delegated so that locking keeps working. (Note tftpy's own TftpServer
    flock=False argument is accepted but never forwarded to the session
    context, so it cannot be used to opt out.)
    """

    def __init__(self, fileobj, done_event):
        self._fileobj = fileobj
        self._done_event = done_event

    def read(self, size=-1):
        chunk = self._fileobj.read(size)
        if size is None or size < 0 or len(chunk) < size:
            self._done_event.set()
        return chunk

    def close(self):
        # tftpy closes the handle once the transfer finishes; belt and braces
        # for a candidate whose length is an exact multiple of the block size.
        self._done_event.set()
        self._fileobj.close()

    def __getattr__(self, item):
        # Only reached for attributes we don't define, so .name / .fileno() /
        # .seek() land on the real file and tftpy's locking is satisfied.
        return getattr(self._fileobj, item)


class ArubaOSCLIDriver(NetworkDriver):
    """Napalm driver for ArubaOSCLI."""

    # Substrings in the switch's copy output that mean the merge did not fully
    # apply. Kept to phrases that are unambiguously failures — a false positive
    # here fails a push that actually worked, so prefer adding observed
    # messages over broad matches like "Error".
    MERGE_ERROR_PATTERNS = (
        "were NOT applied",
        "Configuration file transfer failed",
        "TFTP transfer failed",
        "No such file or directory",
        "Timed out",
    )

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

        # How long to wait for the switch to pull the candidate off us, and how
        # long to keep reading trailing console output after it has.
        self.tftp_transfer_timeout = optional_args.pop("tftp_transfer_timeout", 120)
        self.tftp_settle_time = optional_args.pop("tftp_settle_time", 2)

        # One TFTP server per session, started lazily on the first transfer.
        # TFTP's initial request is fixed at port 69, so only one server can
        # exist per host; standing one up per feature push made consecutive
        # features race each other for the port.
        self._tftp_server = None
        self._tftp_thread = None
        self._tftp_root = None
        self._candidate_dir = None
        self._candidate_path = None
        self._candidate_name = "candidate"
        self._transfer_done = Event()

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
    
    def save_config(self):
        """Save the running config to startup config."""
        self.send_command("copy running-config startup-config")

    def close(self):
        """Implement the NAPALM method close (mandatory)"""
        # Tear the TFTP server down first and unconditionally: if the SSH
        # disconnect raises, the socket must still be released or the next
        # device in the run cannot bind port 69.
        try:
            self._stop_tftp_server()
        finally:
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
        # Stop serving the staged candidate so a stray TFTP request can't pick
        # up config that was explicitly discarded (e.g. after a dry run).
        self._candidate_path = None

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

        for pattern in self.MERGE_ERROR_PATTERNS:
            if pattern in result:
                logger.error("Merge failed on %s: %r", self.hostname, result)
                raise MergeConfigException(
                    "Failed applying config on %s (matched %r):\n%s"
                    % (self.hostname, pattern, result)
                )

    def _transfer_file(self, filecontent, destfile="candidate"):
        """Serve filecontent over TFTP and have the switch merge it.

        The server is reused for the whole session, so successive feature
        pushes no longer contend for port 69 or pay a teardown cost each time.
        """
        self._ensure_tftp_server()
        self._candidate_name = destfile
        self._transfer_done.clear()

        # Stage the candidate as a real file, deliberately NOT inside the
        # served root: tftpy opens root/<name> directly when it exists, which
        # would bypass our handler and lose the completion signal.
        self._candidate_path = os.path.join(self._candidate_dir.name, destfile)
        with open(self._candidate_path, "w") as fobj:
            fobj.write(filecontent)

        logger.info("Sending %d bytes of candidate config to %s",
                    len(filecontent), self.hostname)

        result = self.send_command(
            [f"copy tftp://{self._get_ipaddress()}/{destfile} running-config vrf {self.mgmt_vrf}"]
        )

        # Wait for the switch to actually pull the whole candidate rather than
        # sleeping a fixed interval and hoping. A blind sleep silently
        # truncated larger candidates: the server was torn down mid-transfer
        # and the switch was left with a partially applied config.
        if not self._transfer_done.wait(self.tftp_transfer_timeout):
            raise MergeConfigException(
                "TFTP transfer to %s did not complete within %ss - the running "
                "config may be partially applied"
                % (self.hostname, self.tftp_transfer_timeout)
            )

        # The copy is applied asynchronously, so the switch can print its
        # failure message after the prompt has already come back. Reading the
        # channel for a moment means commit_config actually sees it instead of
        # reporting success on a partial merge.
        result += self._drain_channel(self.tftp_settle_time)

        return result

    def _ensure_tftp_server(self):
        """Start the session's TFTP server if it isn't already running."""
        if self._tftp_server is not None:
            return

        # Served root stays empty; candidates are staged in a separate dir so
        # requests always route through dyn_file_func. See _transfer_file.
        self._tftp_root = tempfile.TemporaryDirectory()
        self._candidate_dir = tempfile.TemporaryDirectory()

        self._tftp_server = tftpy.TftpServer(
            tftproot=self._tftp_root.name,
            dyn_file_func=self._tftp_handler,
        )
        self._tftp_thread = Thread(target=self._tftp_server.listen)
        self._tftp_thread.daemon = True
        self._tftp_thread.start()
        logger.info("TFTP server started for %s session", self.hostname)

    def _stop_tftp_server(self):
        """Stop the session's TFTP server and release port 69."""
        if self._tftp_server is None:
            return

        try:
            self._tftp_server.stop(now=True)
            # tftpy's select() loop has a 5s SOCK_TIMEOUT before it checks the
            # shutdown flag, so allow up to 10s for cleanup.
            self._tftp_thread.join(timeout=10)
            # If the thread still hasn't released the socket, force close it so
            # the next device in the run can bind to port 69.
            if self._tftp_thread.is_alive():
                logger.warning("TFTP thread for %s did not exit; closing socket",
                               self.hostname)
                try:
                    self._tftp_server.sock.close()
                except Exception:
                    pass
        finally:
            for tmpdir in (self._tftp_root, self._candidate_dir):
                if tmpdir is not None:
                    try:
                        tmpdir.cleanup()
                    except Exception:
                        pass
            self._tftp_server = None
            self._tftp_thread = None
            self._tftp_root = None
            self._candidate_dir = None
            self._candidate_path = None

    def _drain_channel(self, seconds):
        """Collect output the switch printed after the prompt came back."""
        extra = ""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = self.device.read_channel()
            except Exception:
                break
            if chunk:
                extra += chunk
                # Keep listening while output is still arriving.
                deadline = time.time() + seconds
            else:
                time.sleep(0.2)
        return extra

    def _tftp_handler(self, fn, raddress=None, rport=None):
        """tftpy dyn_file_func: serve the staged candidate config."""
        if fn != self._candidate_name or not self._candidate_path:
            logger.warning("TFTP request for unexpected file %r from %s", fn, raddress)
            return None
        return _CandidateFile(open(self._candidate_path, "rb"), self._transfer_done)

    def _get_ipaddress(self):
        # Use TFTP_SERVER_IP env var if set, otherwise auto-detect
        if os.environ.get("TFTP_SERVER_IP"):
            return os.environ.get("TFTP_SERVER_IP")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
