"""
tests/integration/test_orchestrator_end_to_end.py

The real stack, together: ProvisioningOrchestrator + EcuUdsClient +
SimulatedEcuServer over real isotp/udp_multicast + CloudProvisioningClient
against a mocked HTTP response. No fakes here — this is what proves steps
2 through 5 actually work as one system, not just individually.
"""
from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock

import pytest

from config import AppConfig, CanBusConfig, CloudApiConfig, UdsAddressConfig
from uds.transport import build_isotp_stack, shutdown
from uds.client import EcuUdsClient
from simulator import SimulatedEcuServer
from cloud import CloudProvisioningClient
from orchestration.workflow import ProvisioningOrchestrator
from reporting import ConsoleStatusReporter


def _test_config(port: int) -> AppConfig:
    return AppConfig(
        can_bus=CanBusConfig(interface="udp_multicast", channel="239.0.0.1", port=port),
        uds_address=UdsAddressConfig(addressing_mode="normal_11bits", rxid=0x7E8, txid=0x7E0),
        cloud_api=CloudApiConfig(
            provision_url="https://api.dev.example.invalid/v1/device/provisioning",
            reprovision_url="https://api.dev.example.invalid/v1/device/re-provisioning",
            api_key="dummy-key",
        ),
        request_timeout_s=2.0,
        model_code="ECX00-V1",
    )


@pytest.fixture
def running_stack():
    cfg = _test_config(port=43_202)  # distinct port from the other integration tests
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


def _mock_cloud_response(vin: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "message": "device provisioned successfully",
        "vin": vin,
        "vin_alias": "1f18bf10-ef10-60c4-b817-9221ddc76d4b",
        "vehicle_model_id": 2,
        "device_id": "1f18bf10-f7b6-6131-b818-9221ddc76d4b",
        "certificate_id": "ccf5b31efdbaeec",
        "device_cert": "-----BEGIN CERTIFICATE-----\ndummycertbytes\n-----END CERTIFICATE-----\n",
        "aws_root_ca": "-----BEGIN CERTIFICATE-----\ndummyrootcabytes\n-----END CERTIFICATE-----\n",
    }
    return resp


def test_full_orchestrator_run_against_real_uds_stack(running_stack):
    cfg, server, uds_client, cloud_client = running_stack
    reporter = ConsoleStatusReporter()
    orchestrator = ProvisioningOrchestrator(uds_client, cloud_client, [reporter], model_code=cfg.model_code)

    vin = "ABC12345678901234"
    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = _mock_cloud_response(vin)
        creds = orchestrator.run(vin)

    assert creds.message == "device provisioned successfully"

    # the ECU actually has everything, byte-for-byte, having gone through the
    # real ISO-TP/UDS wire path end to end — not asserted against a fake
    assert server.state.vin == vin.encode("utf-8")
    assert server.state.device_id == b"1f18bf10-f7b6-6131-b818-9221ddc76d4b"
    assert server.state.vin_alias == b"1f18bf10-ef10-60c4-b817-9221ddc76d4b"
    assert server.state.device_certificate == (
        "-----BEGIN CERTIFICATE-----\ndummycertbytes\n-----END CERTIFICATE-----\n".encode("utf-8")
    )
    assert server.state.root_ca == (
        "-----BEGIN CERTIFICATE-----\ndummyrootcabytes\n-----END CERTIFICATE-----\n".encode("utf-8")
    )
    assert server.state.reset_count == 1