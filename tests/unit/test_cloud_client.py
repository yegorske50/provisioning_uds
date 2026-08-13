"""
tests/unit/test_cloud_client.py

No bus, no simulator — CloudProvisioningClient against a mocked HTTP layer,
using the dummy request/response shapes given for this project (no real dev
API exists yet). Proves: request body construction (headers, JSON shape, csr
base64 serialization), response parsing into Credentials, the str->bytes
conversion on exactly the four UDS-bound fields and nowhere else, retry
behavior on 5xx, and no retry on 4xx.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from config import CloudApiConfig
from cloud import CloudProvisioningClient, CloudApiError, Credentials


def _cfg() -> CloudApiConfig:
    return CloudApiConfig(
        provision_url="https://api.dev.example.invalid/v1/device/provisioning",
        reprovision_url="https://api.dev.example.invalid/v1/device/re-provisioning",
        api_key="dummy-key-for-tests",
        timeout_s=1.0,
        max_retries=3,
    )


def _dummy_response_json() -> dict:
    """Exact shape given for this project, plus deliberately non-ASCII bytes
    inside the cert/root_ca strings ('extra bytes in between') — proves the
    UTF-8 round trip preserves them exactly rather than only working on
    plain-ASCII PEM text."""
    return {
        "message": "device provisioned successfully",
        "vin": "J7V737UBEJ6E6LNRJ",
        "vin_alias": "1f18bf10-ef10-60c4-b817-9221ddc76d4b",
        "vehicle_model_id": 2,
        "device_id": "1f18bf10-f7b6-6131-b818-9221ddc76d4b",
        "certificate_id": "ccf5b31efdbaeec",
        "device_cert": "-----BEGIN CERTIFICATE-----\né\u0000\u00ff\u0080dummycertbytes\n-----END CERTIFICATE-----\n",
        "aws_root_ca": "-----BEGIN CERTIFICATE-----\n\u00e9\u00fedummyrootcabytes\n-----END CERTIFICATE-----\n",
    }


def _mock_ok_response(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def test_provision_request_shape():
    """Headers and JSON body match the given contract exactly, and the csr
    bytes are base64-encoded (not left as raw bytes, which requests.post's
    json= would fail to serialize anyway, and not UTF-8-decoded, which would
    raise on non-UTF-8 CSR content)."""
    client = CloudProvisioningClient(_cfg())
    csr_bytes = bytes([0x00, 0x01, 0x7F, 0x80, 0xFF]) + b"not-necessarily-utf8"

    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = _mock_ok_response(_dummy_response_json())
        client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=csr_bytes)

    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {
        "Content-Type": "application/json",
        "x-api-key": "dummy-key-for-tests",
    }
    body = kwargs["json"]
    assert body["vin"] == "ABC12345678901234"
    assert body["model_code"] == "ECX00-V1"
    import base64
    assert base64.b64decode(body["csr"]) == csr_bytes  # exact round trip through base64


def test_provision_response_parsed_and_converted_to_bytes():
    """The core claim: original cloud string value == bytes handed back in
    Credentials, via UTF-8, for exactly the four UDS-bound fields — including
    the non-ASCII content embedded in this response."""
    client = CloudProvisioningClient(_cfg())
    response_data = _dummy_response_json()

    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = _mock_ok_response(response_data)
        creds = client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=b"\x00\x01\xff")

    assert isinstance(creds, Credentials)

    # UDS-bound fields: bytes, exact UTF-8 encoding of the original string
    assert creds.device_id == response_data["device_id"].encode("utf-8")
    assert creds.vin_alias == response_data["vin_alias"].encode("utf-8")
    assert creds.device_certificate == response_data["device_cert"].encode("utf-8")
    assert creds.root_ca == response_data["aws_root_ca"].encode("utf-8")
    assert isinstance(creds.device_id, bytes)
    assert isinstance(creds.device_certificate, bytes)

    # informational fields: untouched, still str/int, not converted
    assert creds.message == "device provisioned successfully"
    assert creds.vin == "J7V737UBEJ6E6LNRJ"
    assert creds.certificate_id == "ccf5b31efdbaeec"
    assert creds.vehicle_model_id == 2
    assert creds.already_provisioned is False


def test_already_provisioned_response_has_no_certs():
    """The 'already provisioned' response shape genuinely has no
    device_cert/aws_root_ca, AND uses a different key (vin_alias_id, not
    vin_alias) per the source requirements doc — Credentials must handle
    both without crashing on None or a missing key."""
    client = CloudProvisioningClient(_cfg())
    already_provisioned_json = {
        "message": "device already provisioned",
        "device_id": "1f18bf10-f7b6-6131-b818-9221ddc76d4b",
        "thing_id": "some-thing-id",
        "vin_alias_id": "1f18bf10-ef10-60c4-b817-9221ddc76d4b",
    }

    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = _mock_ok_response(already_provisioned_json)
        creds = client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=b"\x00\x01")

    assert creds.already_provisioned is True
    assert creds.device_certificate is None
    assert creds.root_ca is None
    assert creds.vin_alias == b"1f18bf10-ef10-60c4-b817-9221ddc76d4b"  # pulled from vin_alias_id
    assert isinstance(creds.device_id, bytes)  # still converted — this field is always present


def test_5xx_retries_then_succeeds():
    client = CloudProvisioningClient(_cfg())
    fail_response = MagicMock(status_code=503, ok=False, text="server error")
    ok_response = _mock_ok_response(_dummy_response_json())

    with patch("cloud.requests.post") as mock_post, patch("cloud.time.sleep"):
        mock_post.side_effect = [fail_response, fail_response, ok_response]
        creds = client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=b"\x00\x01")

    assert mock_post.call_count == 3
    assert creds.message == "device provisioned successfully"


def test_4xx_does_not_retry():
    client = CloudProvisioningClient(_cfg())
    bad_response = MagicMock(status_code=400, ok=False, text="bad request")

    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = bad_response
        with pytest.raises(CloudApiError):
            client.provision(vin="ABC12345678901234", model_code="ECX00-V1", csr=b"\x00\x01")

    assert mock_post.call_count == 1  # no retry on a 4xx