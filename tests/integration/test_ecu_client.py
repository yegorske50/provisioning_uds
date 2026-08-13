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

    vin = "1HGCM82633A123456"[:17]
    client.write_vin(vin)

    csr = client.read_csr()
    assert csr.startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert len(csr) > 100  # confirms the multi-frame PEM actually came through the codec intact

    client.write_device_id("a" * 36)
    client.write_vin_alias("b" * 36)
    client.write_device_certificate("-----BEGIN CERTIFICATE-----\n" + "x" * 800 + "\n-----END CERTIFICATE-----\n")
    client.write_root_ca("-----BEGIN CERTIFICATE-----\n" + "y" * 800 + "\n-----END CERTIFICATE-----\n")

    client.restart(wait_s=0.2)  # short wait — this is a local simulator, not real hardware
    client.verify_certificate_integrity()  # implicitly re-enters session — this IS "reconnect"


    # simulator's internal state actually has everything we wrote
    assert server.state.vin == vin
    assert server.state.device_id == "a" * 36
    assert server.state.reset_count == 1


def test_verify_before_certs_written_raises(running_simulator):
    """Failure path: routine control on a device that hasn't been fully
    provisioned yet should surface as UdsOperationError, not a silent False."""
    server, client = running_simulator

    client.connect()
    client.write_vin("1HGCM82633A123456"[:17])
    # deliberately skip device_id/alias/cert/root_ca

    with pytest.raises(UdsOperationError):
        client.verify_certificate_integrity()