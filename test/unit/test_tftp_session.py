"""Tests for the session-scoped TFTP server and transfer-completion signal.

These drive a real tftpy client against the driver's own server, so they cover
the wire behaviour rather than mocks. The listen port is patched to an
unprivileged one; production differs only in using port 69.

Two things here are regression guards worth keeping:

* Successive transfers must reuse one server and the listen thread must survive
  all of them. tftpy >= 0.8.5 advisory-locks whatever dyn_file_func returns,
  which raises AttributeError on an in-memory object and kills the listen
  thread — invisible when a server was built per transfer, fatal once it is
  shared for a session. (TftpServer's own flock=False is accepted but never
  forwarded to the session context, so it cannot be used to opt out.)
* A transfer that is never collected must raise, not fall through. The old
  fixed sleep let a truncated transfer look like success while leaving the
  switch with a partially applied config.
"""

import os
import socket
import threading
import time

import pytest
import tftpy

from napalm.base.exceptions import MergeConfigException

from napalm_arubaoscli.arubaoscli import ArubaOSCLIDriver

PORT = 6974


@pytest.fixture
def driver(monkeypatch):
    """A driver with TFTP state initialised but no SSH connection."""
    monkeypatch.setattr(
        tftpy.TftpServer,
        "listen",
        lambda self, listenip="", listenport=PORT, **kw: _listen(
            self, listenip=listenip, listenport=PORT, **kw
        ),
    )

    d = ArubaOSCLIDriver("test-switch", "user", "pass")
    d.mgmt_vrf = "default"
    d._get_ipaddress = lambda: "127.0.0.1"
    d._drain_channel = lambda seconds: ""

    # Issuing the copy command makes the "switch" pull the file in the
    # background, as a real device does.
    state = {"enabled": True, "thread": None, "received": None}

    def send_command(*args, **kwargs):
        if state["enabled"]:
            t = threading.Thread(target=_pull, daemon=True)
            t.start()
            state["thread"] = t
        return "copy in progress"

    def _pull():
        try:
            out = os.path.join(d._candidate_dir.name, "pulled_%f" % time.time())
            tftpy.TftpClient("127.0.0.1", PORT).download("candidate", out)
            with open(out) as fobj:
                state["received"] = fobj.read()
        except Exception as err:  # surfaced via assertions in the test
            state["received"] = "ERROR: %r" % (err,)

    d.send_command = send_command
    d.switch = state

    yield d

    d._stop_tftp_server()


_listen = tftpy.TftpServer.listen


def _fetch(driver, name="candidate"):
    out = os.path.join(driver._candidate_dir.name, "fetch_%f" % time.time())
    tftpy.TftpClient("127.0.0.1", PORT).download(name, out)
    with open(out) as fobj:
        return fobj.read()


CANDIDATES = [
    pytest.param("interface 1/1/1\n    loop-protect vlan 4002\n", id="small"),
    pytest.param(
        "".join(
            "interface 1/1/%d\n    loop-protect vlan 4002\n" % i for i in range(1, 49)
        ),
        id="full-access-port-remediation",
    ),
    pytest.param("interface 1/1/1\n    loop-protect\n" * 4000, id="large"),
    pytest.param("x" * 2048, id="exact-blocksize-multiple"),
]


def test_server_is_started_once_per_session(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    first = id(driver._tftp_server)
    driver._ensure_tftp_server()
    assert id(driver._tftp_server) == first


def test_served_root_stays_empty(driver):
    """Candidates must live outside the served root.

    tftpy opens root/<name> directly when it exists, which would bypass
    dyn_file_func and lose the completion signal.
    """
    driver._ensure_tftp_server()
    driver._candidate_path = None
    with open(os.path.join(driver._candidate_dir.name, "candidate"), "w") as fobj:
        fobj.write("x")
    assert os.listdir(driver._tftp_root.name) == []


@pytest.mark.parametrize("body", CANDIDATES)
def test_transfer_delivers_exact_bytes(driver, body):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    driver._transfer_file(body)
    driver.switch["thread"].join(10)
    assert driver.switch["received"] == body


def test_successive_transfers_reuse_server_and_keep_thread_alive(driver):
    """The per-session behaviour, and the tftpy locking regression."""
    driver._ensure_tftp_server()
    time.sleep(0.5)
    server_id = id(driver._tftp_server)

    for i, param in enumerate(CANDIDATES, 1):
        body = param.values[0]
        driver._transfer_file(body)
        driver.switch["thread"].join(10)
        assert driver.switch["received"] == body, "transfer %d corrupted" % i
        assert id(driver._tftp_server) == server_id, "server rebuilt on %d" % i
        assert driver._tftp_thread.is_alive(), "listen thread died on %d" % i


def test_transfer_returns_on_signal_not_a_fixed_sleep(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    started = time.time()
    driver._transfer_file("interface 1/1/1\n    loop-protect\n")
    # The old implementation slept a flat 5s regardless.
    assert time.time() - started < 4


def test_uncollected_transfer_raises(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    driver.switch["enabled"] = False
    driver.tftp_transfer_timeout = 1
    with pytest.raises(MergeConfigException):
        driver._transfer_file("nobody collects this\n")


def test_unknown_filename_is_refused_without_killing_server(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    driver._transfer_file("interface 1/1/1\n")
    driver.switch["thread"].join(10)

    with pytest.raises(Exception):
        _fetch(driver, "not-the-candidate")

    assert driver._tftp_thread.is_alive()
    assert _fetch(driver) == "interface 1/1/1\n"


def test_discard_config_stops_serving_candidate(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    driver._transfer_file("interface 1/1/1\n")
    driver.switch["thread"].join(10)

    driver.discard_config()
    with pytest.raises(Exception):
        _fetch(driver)


def test_stop_releases_port_and_is_idempotent(driver):
    driver._ensure_tftp_server()
    time.sleep(0.5)
    driver._stop_tftp_server()

    assert driver._tftp_server is None
    assert driver._tftp_root is None
    assert driver._candidate_dir is None

    driver._stop_tftp_server()  # must not raise

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", PORT))
    finally:
        sock.close()


@pytest.mark.parametrize(
    "output",
    [
        "Some of the configuration lines from the file were NOT applied",
        "TFTP transfer failed",
        "Timed out",
    ],
)
def test_commit_raises_on_failure_output(driver, monkeypatch, output):
    driver.merge_candidate = "interface 1/1/1\n"
    monkeypatch.setattr(driver, "_transfer_file", lambda *a, **k: output)
    with pytest.raises(MergeConfigException):
        driver.commit_config()


def test_commit_accepts_clean_output(driver, monkeypatch):
    driver.merge_candidate = "interface 1/1/1\n"
    monkeypatch.setattr(
        driver, "_transfer_file", lambda *a, **k: "copy completed successfully"
    )
    driver.commit_config()
