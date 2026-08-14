"""
fake_cloud.py — local stand-in for the In-House Cloud Device Provisioning
API, so the full provisioning flow can run without a real cloud endpoint.

Implements POST /v1/device/provisioning matching exactly what
CloudProvisioningClient.provision() (cloud.py) sends and expects: request
body {vin, model_code, csr}, response {message, vin, vin_alias,
vehicle_model_id, device_id, certificate_id, device_cert, aws_root_ca} — the
same shape as the reference requirements doc's "new provision" response.
Nothing in cloud.py, uds/, or orchestration/ changes or even knows this
exists — CloudProvisioningClient already only knows "POST this URL", and
that URL is fully config-driven (CloudApiConfig.provision_url, set via
PROVISIONING_PROVISION_URL). Pointing at this instead of a real API — or
switching back later — is a .env change only.

Only the "new provision" (success) response shape is implemented. The
"already provisioned" branch is already covered by cloud.py's own mocked
unit tests (tests/unit/test_cloud_client.py) and doesn't need a live
endpoint to exercise — adding it here would be a second, harder-to-keep-
consistent copy of the same test data.

Run standalone:
    python fake_cloud.py [--host 127.0.0.1] [--port 8000]

Then point config at it (see .env.dev.example):
    PROVISIONING_PROVISION_URL=http://127.0.0.1:8000/v1/device/provisioning
"""

from __future__ import annotations

import argparse
import base64
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Fake Cloud Provisioning API")


class ProvisionRequest(BaseModel):
    vin: str
    model_code: str
    csr: str


def _deterministic_id(*parts: str) -> str:
    """Same input always produces the same output — not random — so a
    given VIN's response is reproducible across runs, while different VINs
    get genuinely different values (uuid5 is namespace+name based, not a
    counter or random uuid4)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, ":".join(parts)))


def _dummy_pem(vin: str, salt: str) -> str:
    body = _deterministic_id(vin, salt).replace("-", "")
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


@app.post("/v1/device/provisioning")
def provision(request: ProvisionRequest, x_api_key: str | None = Header(default=None, alias="x-api-key")):
    # Deliberately not enforcing x_api_key here, even though a real cloud API
    # would. .env.dev.example leaves PROVISIONING_API_KEY blank on purpose
    # ("blank is fine until you're testing against a real key") — rejecting
    # that here would contradict a convention this project already settled
    # on, and break this server against the exact .env the project ships.
    try:
        base64.b64decode(request.csr, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="csr is not valid base64")

    device_id = _deterministic_id(request.vin, "device_id")
    vin_alias = _deterministic_id(request.vin, "vin_alias")
    certificate_id = _deterministic_id(request.vin, "certificate_id").replace("-", "")[:15]

    return {
        "message": "device provisioned successfully",
        "vin": request.vin,
        "vin_alias": vin_alias,
        "vehicle_model_id": 2,
        "device_id": device_id,
        "certificate_id": certificate_id,
        "device_cert": _dummy_pem(request.vin, "device_cert"),
        "aws_root_ca": _dummy_pem(request.vin, "aws_root_ca"),
    }


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Fake Cloud Provisioning API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print(f"Fake cloud API listening on http://{args.host}:{args.port}/v1/device/provisioning")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()