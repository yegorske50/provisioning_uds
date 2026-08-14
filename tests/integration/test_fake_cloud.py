"""
tests/integration/test_fake_cloud.py

Runs fake_cloud.py as a real uvicorn server on a local port, then points the
REAL CloudProvisioningClient at it — no mocking of `requests.post`, unlike
tests/unit/test_cloud_client.py. This is what actually proves fake_cloud.py
is wire-compatible with what CloudProvisioningClient sends and expects, not
just that fake_cloud.py's own logic looks right in isolation.
"""
from __future__ import annotations

import threading
import time

import pytest
import requests
import uvicorn

from config import CloudApiConfig
from cloud import CloudProvisioningClient
import fake_cloud


@pytest.fixture
def running_fake_cloud():
    port = 8901  # dedicated test port, distinct from the default 8000
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


def _cfg(base_url: str) -> CloudApiConfig:
    return CloudApiConfig(
        provision_url=f"{base_url}/v1/device/provisioning",
        reprovision_url=f"{base_url}/v1/device/re-provisioning",
        api_key="dummy-key",
        timeout_s=2.0,
        max_retries=1,
    )


def test_real_client_against_real_fake_server(running_fake_cloud):
    client = CloudProvisioningClient(_cfg(running_fake_cloud))
    csr = bytes([0x00, 0x01, 0x7F, 0x80, 0xFF]) + b"not-necessarily-utf8"

    creds = client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=csr)

    assert creds.message == "device provisioned successfully"
    assert creds.already_provisioned is False
    assert isinstance(creds.device_id, bytes)
    assert isinstance(creds.vin_alias, bytes)
    assert isinstance(creds.device_certificate, bytes)
    assert isinstance(creds.root_ca, bytes)
    assert creds.device_certificate.startswith(b"-----BEGIN CERTIFICATE-----")


def test_same_vin_gets_deterministic_response(running_fake_cloud):
    client = CloudProvisioningClient(_cfg(running_fake_cloud))
    creds1 = client.provision(vin="SAMEVIN12345", model_code="ECX00-V1", csr=b"\x00\x01")
    creds2 = client.provision(vin="SAMEVIN12345", model_code="ECX00-V1", csr=b"\x00\x01")

    assert creds1.device_id == creds2.device_id
    assert creds1.vin_alias == creds2.vin_alias
    assert creds1.device_certificate == creds2.device_certificate


def test_different_vins_get_different_device_ids(running_fake_cloud):
    client = CloudProvisioningClient(_cfg(running_fake_cloud))
    creds1 = client.provision(vin="VIN0000000001", model_code="ECX00-V1", csr=b"\x00")
    creds2 = client.provision(vin="VIN0000000002", model_code="ECX00-V1", csr=b"\x00")

    assert creds1.device_id != creds2.device_id
    assert creds1.vin_alias != creds2.vin_alias


def test_blank_api_key_still_works(running_fake_cloud):
    """Matches .env.dev.example's convention — blank key must not be
    rejected, since that's how this project's own dev config ships."""
    cfg = _cfg(running_fake_cloud)
    cfg = CloudApiConfig(
        provision_url=cfg.provision_url, reprovision_url=cfg.reprovision_url,
        api_key="", timeout_s=cfg.timeout_s, max_retries=cfg.max_retries,
    )
    client = CloudProvisioningClient(cfg)
    creds = client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=b"\x00")
    assert creds.message == "device provisioned successfully"


def test_invalid_base64_csr_rejected(running_fake_cloud):
    resp = requests.post(
        f"{running_fake_cloud}/v1/device/provisioning",
        json={"vin": "X", "model_code": "Y", "csr": "not valid base64!!"},
        headers={"x-api-key": "k"},
        timeout=2,
    )
    assert resp.status_code == 400