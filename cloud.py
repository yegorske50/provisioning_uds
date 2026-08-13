"""
cloud.py — Credentials model + CloudProvisioningClient.provision().

Talks to the In-House Cloud Device Provisioning API (FR-014..017). There's no
real dev URL yet, so this is built against the exact dummy request/response
shapes given for this project — provision() always makes a real HTTP call
(via `requests`); tests mock `requests.post` rather than this file branching
on "is there a real URL". Swapping to the real endpoint later only means
pointing config at it — nothing here changes.

Credentials.device_id / .vin_alias / .device_certificate / .root_ca are
`bytes`, not `str`, even though the cloud API returns JSON strings for all of
them. This is the one UTF-8 encode this file performs on the response side,
and it's deliberate: JSON has no bytes type, so *some* str->bytes step is
unavoidable to get UDS-transmittable bytes at all. UTF-8 is safe here
specifically because JSON strings are guaranteed valid Unicode by the JSON
spec — nothing is corrupted, guessed at, or reformatted; every code point
survives as its exact UTF-8 byte sequence. Fields that never reach UDS
(message, the cloud's vin echo, certificate_id, vehicle_model_id) are left
exactly as JSON gives them — no reason to touch them.

The request direction has the mirror-image problem: `csr` arrives as `bytes`
(from EcuUdsClient.read_csr()) and has to go *into* JSON. UTF-8 doesn't work
here — CSR bytes aren't guaranteed valid UTF-8, that's the entire reason it's
`bytes` and not `str` in the first place. base64 is used instead, purely as
a transport-safe container for arbitrary binary in a text protocol — this is
NOT the same thing as assuming/decoding an incoming base64 format (forbidden
by the cloud-data requirement); it's this application choosing how to encode
its own outgoing bytes, which is unavoidable for any binary-in-JSON payload.
Flagged as a placeholder pending real API confirmation — see provision()'s
docstring and the report at the end of this turn.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field

import requests

from config import CloudApiConfig

logger = logging.getLogger(__name__)


class CloudApiError(Exception):
    """Everything provision() can fail on — network errors, non-2xx status,
    malformed JSON, or a response missing fields the caller needs. ER-004."""


@dataclass
class Credentials:
    message: str
    already_provisioned: bool

    # UDS-bound — bytes, ready to hand directly to EcuUdsClient methods
    device_id: bytes
    vin_alias: bytes
    device_certificate: bytes | None    # None on "already provisioned" — Open Question #3
    root_ca: bytes | None               # None on "already provisioned"

    # informational only — never written to the ECU, left as JSON gave them
    certificate_id: str | None = None
    vin: str | None = None              # the cloud's echo of the submitted VIN
    vehicle_model_id: int | None = None
    disabled_certificates: list = field(default_factory=list)  # reprovision only, unused for now


class CloudProvisioningClient:
    def __init__(self, cfg: CloudApiConfig):
        self._cfg = cfg

    def provision(self, vin: str, model_code: str, csr: bytes) -> Credentials:
        """FR-014..017. POSTs to the provisioning endpoint, normalizes either
        response shape (new provision / already provisioned) into
        Credentials, with the four UDS-bound fields already converted to
        bytes.

        `vin` and `model_code` are plain str — they're JSON request fields
        here, not UDS payloads (the VIN was already written to the ECU
        earlier in the flow, using the orchestrator's own copy — this method
        never touches UDS). `csr` is bytes, matching what read_csr() actually
        produces, and gets base64-encoded into the request body — see this
        module's docstring for why, and why that specific choice is still
        unconfirmed against a real API.
        """
        body = {
            "vin": vin,
            "model_code": model_code,
            "csr": base64.b64encode(csr).decode("ascii"),  # base64 alphabet is ASCII by construction — always safe
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._cfg.api_key,
        }

        response_json = self._post_with_retry(self._cfg.provision_url, headers, body)
        return self._parse_response(response_json)

    def _post_with_retry(self, url: str, headers: dict, body: dict) -> dict:
        """Retries on network errors and 5xx only — never on 4xx, since
        retrying a client error just fails the same way again."""
        last_error: Exception | None = None
        for attempt in range(self._cfg.max_retries):
            try:
                response = requests.post(url, headers=headers, json=body, timeout=self._cfg.timeout_s)
            except requests.RequestException as e:
                last_error = e
                logger.warning("Cloud API request failed (attempt %d/%d): %s", attempt + 1, self._cfg.max_retries, e)
                time.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code >= 500:
                last_error = CloudApiError(f"Server error {response.status_code}: {response.text}")
                logger.warning("Cloud API 5xx (attempt %d/%d): %s", attempt + 1, self._cfg.max_retries, last_error)
                time.sleep(0.5 * (attempt + 1))
                continue

            if not response.ok:
                raise CloudApiError(f"Cloud API returned {response.status_code}: {response.text}")

            try:
                return response.json()
            except ValueError as e:
                raise CloudApiError(f"Cloud API returned invalid JSON: {response.text}") from e

        raise CloudApiError(f"Cloud API request failed after {self._cfg.max_retries} attempts: {last_error}")

    def _parse_response(self, data: dict) -> Credentials:
        message = data.get("message", "")
        # exact wording of the "already provisioned" message isn't confirmed
        # against a real API yet — substring match is deliberately loose
        # rather than an exact-equality guess. Open Question #3.
        already_provisioned = "already provisioned" in message.lower()

        device_id = data.get("device_id")
        if device_id is None:
            raise CloudApiError(f"Cloud response missing device_id: {data!r}")

        if already_provisioned:
            # Per the source doc, this shape is device_id/thing_id/vin_alias_id
            # — no device_cert/aws_root_ca, and a DIFFERENT key name for the
            # alias than the "new provision" shape uses. Falling back to
            # "vin_alias" too since it's genuinely unconfirmed which key the
            # real API will actually use here — Open Question #3.
            vin_alias = data.get("vin_alias_id") or data.get("vin_alias")
            device_cert = None
            aws_root_ca = None
        else:
            vin_alias = data.get("vin_alias")
            device_cert = data.get("device_cert")
            aws_root_ca = data.get("aws_root_ca")

        if vin_alias is None:
            raise CloudApiError(f"Cloud response missing vin_alias/vin_alias_id: {data!r}")

        return Credentials(
            message=message,
            already_provisioned=already_provisioned,
            device_id=device_id.encode("utf-8"),
            vin_alias=vin_alias.encode("utf-8"),
            device_certificate=device_cert.encode("utf-8") if device_cert is not None else None,
            root_ca=aws_root_ca.encode("utf-8") if aws_root_ca is not None else None,
            certificate_id=data.get("certificate_id"),
            vin=data.get("vin"),
            vehicle_model_id=data.get("vehicle_model_id"),
        )