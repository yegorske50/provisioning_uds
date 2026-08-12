"""
tests/integration/test_simulator_transport.py

End-to-end wire-level test: real isotp.CanStack + udp_multicast on both sides,
SimulatedEcuServer on one, raw UDS byte sequences driven from the test on the
other. No udsoncan.Client and no EcuUdsClient yet — those are step 3. This
test exists purely to prove uds/transport.py + simulator.py are correct at
the protocol level before anything gets built on top of them.
"""
from __future__ import annotations

import threading
import time

import pytest

from config import AppConfig, CanBusConfig, CloudApiConfig, UdsAddressConfig
from uds.transport import build_isotp_stack, shutdown
from simulator import SimulatedEcuServer


def _test_config(port: int) -> AppConfig:
    # Distinct port from the dev default (43113) so this doesn't collide with
    # anything else running udp_multicast on the same machine.
    return AppConfig(
        can_bus=CanBusConfig(interface="udp_multicast", channel="239.0.0.1", port=port),
        uds_address=UdsAddressConfig(addressing_mode="normal_11bits", rxid=0x7E8, txid=0x7E0),
        cloud_api=CloudApiConfig(provision_url="", reprovision_url="", api_key=""),
    )


@pytest.fixture
def running_simulator():
    cfg = _test_config(port=43_200)
    ecu_stack = build_isotp_stack(cfg, role="ecu")
    server = SimulatedEcuServer(ecu_stack)
    server.SESSION_TIMEOUT_S = 1.0  # short, so the timeout test doesn't take 5+ seconds

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client_stack = build_isotp_stack(cfg, role="client")
    client_stack.start()  # not using udsoncan.Client here, so we start it ourselves

    yield server, client_stack

    server.stop()
    shutdown(client_stack)
    shutdown(ecu_stack)


def _send_recv(stack, payload: bytes, timeout: float = 2.0) -> bytes:
    stack.send(payload)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = stack.recv()
        if resp is not None:
            return resp
        time.sleep(0.01)
    raise TimeoutError(f"No response to {payload.hex()}")


def test_full_happy_path(running_simulator):
    server, client = running_simulator

    assert _send_recv(client, bytes([0x10, 0x03])) == bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])

    vin = b"1HGCM82633A123456"[:17]
    assert len(vin) == 17
    assert _send_recv(client, bytes([0x2E, 0xF1, 0x90]) + vin) == bytes([0x6E, 0xF1, 0x90])

    csr_resp = _send_recv(client, bytes([0x22, 0xF1, 0xA0]))
    assert csr_resp[:3] == bytes([0x62, 0xF1, 0xA0])
    csr_bytes = csr_resp[3:]
    assert len(csr_bytes) > 7  # proves ISO-TP multi-frame actually carried this
    assert csr_bytes.startswith(b"-----BEGIN CERTIFICATE REQUEST-----")

    device_id = b"a" * 36
    assert _send_recv(client, bytes([0x2E, 0xF1, 0xA1]) + device_id) == bytes([0x6E, 0xF1, 0xA1])

    vin_alias = b"b" * 36
    assert _send_recv(client, bytes([0x2E, 0xF1, 0xA2]) + vin_alias) == bytes([0x6E, 0xF1, 0xA2])

    cert = b"-----BEGIN CERTIFICATE-----\n" + b"x" * 800 + b"\n-----END CERTIFICATE-----\n"
    assert _send_recv(client, bytes([0x2E, 0xF1, 0xA3]) + cert) == bytes([0x6E, 0xF1, 0xA3])

    root_ca = b"-----BEGIN CERTIFICATE-----\n" + b"y" * 800 + b"\n-----END CERTIFICATE-----\n"
    assert _send_recv(client, bytes([0x2E, 0xF1, 0xA4]) + root_ca) == bytes([0x6E, 0xF1, 0xA4])

    assert _send_recv(client, bytes([0x31, 0x01, 0x03, 0x01])) == bytes([0x71, 0x01, 0x03, 0x01])

    assert _send_recv(client, bytes([0x11, 0x01])) == bytes([0x51, 0x01])

    # ECUReset drops the session back to default — an extended-only op now fails
    resp = _send_recv(client, bytes([0x22, 0xF1, 0xA0]))
    assert resp == bytes([0x7F, 0x22, 0x7F])  # ServiceNotSupportedInActiveSession

    assert _send_recv(client, bytes([0x10, 0x01]))[:2] == bytes([0x50, 0x01])  # FR-039


def test_session_times_out_and_resend_10_03_fixes_it(running_simulator):
    """The exact scenario from the TesterPresent discussion: sit idle past the
    session timeout (standing in for the cloud API wait), confirm the write
    fails without a fresh 10 03, then confirm it succeeds with one."""
    server, client = running_simulator

    _send_recv(client, bytes([0x10, 0x03]))  # enter extended session

    time.sleep(server.SESSION_TIMEOUT_S + 0.5)  # simulate the cloud API gap

    device_id = b"c" * 36
    resp = _send_recv(client, bytes([0x2E, 0xF1, 0xA1]) + device_id)
    assert resp == bytes([0x7F, 0x2E, 0x7F])  # session silently dropped — write rejected

    # the agreed fix: re-send 10 03 before the write-sensitive step
    _send_recv(client, bytes([0x10, 0x03]))
    resp = _send_recv(client, bytes([0x2E, 0xF1, 0xA1]) + device_id)
    assert resp == bytes([0x6E, 0xF1, 0xA1])  # now it works