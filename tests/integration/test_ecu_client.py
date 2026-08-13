"""
tests/integration/test_ecu_client.py

Full happy path through EcuUdsClient (not raw bytes this time) against
SimulatedEcuServer — proves uds/codecs.py + uds/client.py work correctly on
top of the transport/simulator layer verified in test_simulator_transport.py.
"""
from __future__ import annotations

import threading

import pytest

from config import AppConfig, CanBusConfig, CloudApiConfig, UdsAddressConfig
from uds.transport import build_isotp_stack, shutdown
from uds.client import EcuUdsClient, UdsOperationError
from simulator import SimulatedEcuServer


def _test_config(port: int) -> AppConfig:
    return AppConfig(
        can_bus=CanBusConfig(interface="udp_multicast", channel="239.0.0.1", port=port),
        uds_address=UdsAddressConfig(addressing_mode="normal_11bits", rxid=0x7E8, txid=0x7E0),
        cloud_api=CloudApiConfig(provision_url="", reprovision_url="", api_key=""),
        request_timeout_s=2.0,
    )


@pytest.fixture
def running_simulator():
    cfg = _test_config(port=43_201)  # different port than test_simulator_transport.py
    ecu_stack = build_isotp_stack(cfg, role="ecu")
    server = SimulatedEcuServer(ecu_stack)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client_stack = build_isotp_stack(cfg, role="client")
    # not calling client_stack.start() here — EcuUdsClient.connect() does it
    # via udsoncan's `client.open()`, which is the whole reason transport.py
    # leaves the client-role stack unstarted.

    client = EcuUdsClient(client_stack, cfg)

    yield server, client

    client.disconnect()  # must run BEFORE the ECU's bus goes away — it still needs to talk to it
    shutdown(client_stack)
    server.stop()
    shutdown(ecu_stack)


def test_full_happy_path_via_client(running_simulator):
    server, client = running_simulator

    client.connect()

    vin = b"1HGCM82633A123456"  # length is whatever the cloud sends — not forced to 17
    client.write_vin(vin)

    csr = client.read_csr()
    assert isinstance(csr, bytes)  # not str — see uds/codecs.py RawBytesCodec
    assert csr == server.state.csr  # exact bytes, not "looks about right"
    assert len(csr) > 100  # confirms the multi-frame transfer actually came through the codec intact

    device_id = b"a" * 36  # 36 is incidental here, not enforced — see test_no_length_or_format_assumptions
    vin_alias = b"b" * 36
    client.write_device_id(device_id)
    client.write_vin_alias(vin_alias)
    # every byte value 0x00-0xFF, not printable ASCII — proves no hidden
    # encode/decode step is quietly surviving on "nice" data only
    client.write_device_certificate(bytes(range(256)) * 4)
    client.write_root_ca(bytes(reversed(range(256))) * 4)

    client.restart(wait_s=0.2)  # short wait — this is a local simulator, not real hardware
    client.verify_certificate_integrity()  # implicitly re-enters session — this IS "reconnect"

    # simulator's internal state actually has everything we wrote, byte-for-byte
    assert server.state.vin == vin
    assert server.state.device_id == device_id
    assert server.state.device_certificate == bytes(range(256)) * 4
    assert server.state.root_ca == bytes(reversed(range(256))) * 4
    assert server.state.reset_count == 1


def test_raw_bytes_survive_full_round_trip(running_simulator):
    """The specific claim this change makes: opaque bytes go in one end and
    come out the other completely unchanged, including values that a hidden
    ASCII/string conversion would corrupt (0x80-0xFF) or crash on outright.
    application -> EcuUdsClient -> UDS -> simulator -> UDS -> EcuUdsClient."""
    server, client = running_simulator

    client.connect()
    client.write_vin(b"1HGCM82633A123456")

    # READ direction: simulator generates the CSR internally; verify the exact
    # bytes it produced survive back out through EcuUdsClient unchanged.
    csr = client.read_csr()
    assert isinstance(csr, bytes)
    assert csr == server.state.csr
    assert 0x00 in csr and 0x01 in csr and 0xFF in csr and 0x80 in csr

    # WRITE direction: application supplies every byte value 0x00-0xFF;
    # verify what actually landed in the simulator is byte-for-byte identical.
    cert_bytes = bytes(range(256)) * 4
    client.write_device_certificate(cert_bytes)
    assert isinstance(server.state.device_certificate, bytes)
    assert server.state.device_certificate == cert_bytes

    root_ca_bytes = bytes(reversed(range(256))) * 4
    client.write_root_ca(root_ca_bytes)
    assert server.state.root_ca == root_ca_bytes


def test_verify_before_certs_written_raises(running_simulator):
    """Failure path: routine control on a device that hasn't been fully
    provisioned yet should surface as UdsOperationError, not a silent False."""
    server, client = running_simulator

    client.connect()
    client.write_vin(b"1HGCM82633A123456")
    # deliberately skip device_id/alias/cert/root_ca

    with pytest.raises(UdsOperationError):
        client.verify_certificate_integrity()


def test_no_length_or_format_assumptions(running_simulator):
    """The specific claim of this change: VIN/device_id/vin_alias get no
    special treatment for being 'commonly strings'. A short value isn't
    padded, a non-ASCII value isn't corrupted or rejected, and nothing gets
    silently modified to fit an assumed length (17 for VIN, 36 for the
    others) that the actual ECU spec never defined."""
    server, client = running_simulator
    client.connect()

    # short VIN, nowhere near 17 bytes — must survive completely unmodified,
    # specifically NOT padded to any length
    short_vin = b"ABC"
    client.write_vin(short_vin)
    assert server.state.vin == short_vin
    assert len(server.state.vin) == 3  # proves no padding happened

    # non-ASCII bytes in a field that used to be ASCII-only — must not be
    # corrupted (the old StringCodec's errors="replace" would have mangled
    # this) and must not be rejected for "not looking like a VIN"
    weird_vin = bytes([0x00, 0x01, 0x7F, 0x80, 0xFF]) + b"VIN"
    client.write_vin(weird_vin)
    assert server.state.vin == weird_vin

    # device_id / vin_alias: same story, deliberately not 36 bytes
    short_device_id = b"x"
    client.write_device_id(short_device_id)
    assert server.state.device_id == short_device_id

    long_vin_alias = b"y" * 200  # deliberately longer than the old 36-byte assumption
    client.write_vin_alias(long_vin_alias)
    assert server.state.vin_alias == long_vin_alias

    # the one thing still rejected: a genuinely empty payload — a protocol-
    # level sanity check, not a content-format assumption about any field
    with pytest.raises(UdsOperationError):
        client.write_vin(b"")