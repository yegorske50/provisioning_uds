"""
tests/integration/test_full_flow_with_fake_cloud.py

The actual thing this deliverable is for: ProvisioningOrchestrator +
EcuUdsClient + SimulatedEcuServer (real isotp/udp_multicast) +
CloudProvisioningClient + fake_cloud.py (real uvicorn server). No mocks
anywhere in this file — everything is a real object talking to another real
object, entirely locally. This is what "run the complete provisioning flow
without the real cloud API" actually means, proven rather than asserted.
"""
from __future__ import annotations

import threading
import time

import pytest
import requests
import uvicorn

from config import AppConfig, CanBusConfig, CloudApiConfig, UdsAddressConfig
from uds.transport import build_isotp_stack, shutdown
from uds.client import EcuUdsClient
from simulator import SimulatedEcuServer
from cloud import CloudProvisioningClient
from orchestration.workflow import ProvisioningOrchestrator
from reporting import ConsoleStatusReporter
import fake_cloud


@pytest.fixture
def running_fake_cloud():
    port = 8902  # distinct from test_fake_cloud.py's port
    config = uvicorn.Config(fake_cloud.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{port}/openapi.json", timeout=0.2)
            break
        except requests.ConnectionError:
            time.sleep(0.05)
    else:
        pytest.fail("fake_cloud server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=2)


@pytest.fixture
def running_stack(running_fake_cloud):
    cfg = AppConfig(
        can_bus=CanBusConfig(interface="udp_multicast", channel="239.0.0.1", port=43_203),  # distinct port
        uds_address=UdsAddressConfig(addressing_mode="normal_11bits", rxid=0x7E8, txid=0x7E0),
        cloud_api=CloudApiConfig(
            provision_url=f"{running_fake_cloud}/v1/device/provisioning",
            reprovision_url=f"{running_fake_cloud}/v1/device/re-provisioning",
            api_key="",  # matches .env.dev.example's convention
        ),
        request_timeout_s=2.0,
        model_code="ECX00-V1",
    )
    ecu_stack = build_isotp_stack(cfg, role="ecu")
    server = SimulatedEcuServer(ecu_stack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client_stack = build_isotp_stack(cfg, role="client")
    uds_client = EcuUdsClient(client_stack, cfg)
    cloud_client = CloudProvisioningClient(cfg.cloud_api)

    yield cfg, server, uds_client, cloud_client

    server.stop()
    shutdown(ecu_stack)
    shutdown(client_stack)


def test_complete_flow_no_mocks(running_stack):
    cfg, server, uds_client, cloud_client = running_stack
    orchestrator = ProvisioningOrchestrator(uds_client, cloud_client, [ConsoleStatusReporter()], model_code=cfg.model_code)

    vin = "ZZZ98765432101234"
    creds = orchestrator.run(vin)

    assert creds.message == "device provisioned successfully"

    # the ECU genuinely has what the fake cloud generated, having gone
    # through real ISO-TP wire traffic and a real HTTP round trip — not
    # asserted against anything either side pre-agreed on
    assert server.state.vin == vin.encode("utf-8")
    assert server.state.device_id == creds.device_id
    assert server.state.vin_alias == creds.vin_alias
    assert server.state.device_certificate == creds.device_certificate
    assert server.state.root_ca == creds.root_ca
    assert server.state.reset_count == 1