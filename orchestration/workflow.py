"""
orchestration/workflow.py — ProvisioningOrchestrator: the state machine.

Depends on UdsClientProtocol / CloudClientProtocol, never a concrete class —
this is the mechanical reason "same app, different hardware" holds: this
module cannot reference isotp/can/requests even by accident. There is still
only one concrete EcuUdsClient and one concrete CloudProvisioningClient; the
Protocols exist so this file doesn't know or care that they exist, and so
tests run against fakes with no bus and no network.

Two known rough edges, flagged rather than smoothed over:

1. Already-provisioned handling (Open Question #3): that response shape has
   no device_cert/aws_root_ca, so there's nothing to program. run() raises
   rather than guessing what "success" means here — needs a product decision.

2. ER-006 vs ER-007 ambiguity: verify_certificate_integrity() on EcuUdsClient
   does BOTH "reconnect" (re-enter extended session) and "verify" (routine
   control) behind one call (see uds/client.py's docstring on why). A failure
   there could be either FR-032/033 (reconnect) or FR-035..038 (verify) and
   this file cannot tell which without EcuUdsClient exposing more granular
   methods. Defaults to ER-006, since a device still booting fails at session
   entry before ever reaching the routine control — more likely, not certain.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Protocol

from cloud import Credentials, CloudApiError
from orchestration.errors import ProvisioningError
from orchestration.states import ProvisioningStage, ProvisioningEvent
from reporting import StatusReporter
from uds.client import UdsOperationError

logger = logging.getLogger(__name__)


class UdsClientProtocol(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def write_vin(self, vin: bytes) -> None: ...
    def read_csr(self) -> bytes: ...
    def write_device_id(self, device_id: bytes) -> None: ...
    def write_vin_alias(self, alias: bytes) -> None: ...
    def write_device_certificate(self, data: bytes) -> None: ...
    def write_root_ca(self, data: bytes) -> None: ...
    def restart(self, wait_s: float = 2.0) -> None: ...
    def verify_certificate_integrity(self) -> None: ...


class CloudClientProtocol(Protocol):
    def provision(self, vin: str, model_code: str, csr: bytes) -> Credentials: ...


class ProvisioningOrchestrator:
    def __init__(
        self,
        uds_client: UdsClientProtocol,
        cloud_client: CloudClientProtocol,
        reporters: list[StatusReporter],
        model_code: str,
    ):
        self._uds_client = uds_client
        self._cloud_client = cloud_client
        self._reporters = reporters
        self._model_code = model_code
        self._current_vin: str = ""  # set at the start of run(); single-device-at-a-time flow, see doc

    def run(self, vin: str) -> Credentials:
        """Runs FR-002 through FR-040 in order. Returns Credentials on
        success. Raises ProvisioningError(er_code=...) on failure — every
        ER-00x gets the same "terminate and report" treatment (the doc never
        says "retry the step"), handled once here rather than duplicated in
        every private step."""
        self._current_vin = vin
        try:
            self._emit(ProvisioningStage.WAITING_FOR_DEVICE)
            self._connect()
            self._program_vin(vin)
            csr = self._retrieve_csr()
            creds = self._acquire_credentials(vin, csr)

            if creds.already_provisioned:
                raise ProvisioningError(
                    "ER-004",
                    ProvisioningStage.ALREADY_PROVISIONED,
                    "Cloud reports device already provisioned but returned "
                    "no certificate data to program — architecture doc Open "
                    "Question #3, needs a product decision, not a guess.",
                )

            self._program_credentials(creds)
            self._restart_and_reconnect()
            self._verify_integrity()
            self._disconnect()
            self._emit(ProvisioningStage.SUCCESS)
            return creds
        except ProvisioningError as e:
            self._emit(ProvisioningStage.FAILED, detail=f"{e.er_code}: {e.message}")
            raise

    # --- private steps — one per FR group -------------------------------------

    def _connect(self) -> None:
        try:
            self._uds_client.connect()
        except UdsOperationError as e:
            raise ProvisioningError("ER-001", ProvisioningStage.FAILED, f"Device connection failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.DEVICE_CONNECTED)

    def _program_vin(self, vin: str) -> None:
        # UTF-8 is the minimum-necessary serialization from the operator-
        # provided VIN string to UDS-transmittable bytes — same reasoning as
        # cloud.py's response-side conversion.
        try:
            self._uds_client.write_vin(vin.encode("utf-8"))
        except UdsOperationError as e:
            raise ProvisioningError("ER-002", ProvisioningStage.FAILED, f"VIN programming failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.VIN_PROGRAMMED)

    def _retrieve_csr(self) -> bytes:
        try:
            csr = self._uds_client.read_csr()
        except UdsOperationError as e:
            raise ProvisioningError("ER-003", ProvisioningStage.FAILED, f"CSR retrieval failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.CSR_RECEIVED)
        return csr

    def _acquire_credentials(self, vin: str, csr: bytes) -> Credentials:
        self._emit(ProvisioningStage.CALLING_CLOUD_API)
        try:
            creds = self._cloud_client.provision(vin, self._model_code, csr)
        except CloudApiError as e:
            raise ProvisioningError("ER-004", ProvisioningStage.FAILED, f"Cloud API call failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.CREDENTIALS_RECEIVED)
        return creds

    def _program_credentials(self, creds: Credentials) -> None:
        if creds.device_certificate is None or creds.root_ca is None:
            # shouldn't happen — run() already gates on already_provisioned
            # before calling this — but the already_provisioned heuristic in
            # cloud.py is a substring match on `message`, not guaranteed, so
            # this is a real safety net, not paranoia for its own sake.
            raise ProvisioningError(
                "ER-004", ProvisioningStage.FAILED,
                "No certificate data to program — see architecture doc Open Question #3.",
            )
        try:
            self._uds_client.write_device_id(creds.device_id)
            self._emit(ProvisioningStage.DEVICE_ID_PROGRAMMED)
            self._uds_client.write_vin_alias(creds.vin_alias)
            self._emit(ProvisioningStage.VIN_ALIAS_PROGRAMMED)
            self._uds_client.write_device_certificate(creds.device_certificate)
            self._uds_client.write_root_ca(creds.root_ca)
            self._emit(ProvisioningStage.CERTIFICATES_PROGRAMMED)
        except UdsOperationError as e:
            raise ProvisioningError("ER-005", ProvisioningStage.FAILED, f"Credential programming failed: {e}", cause=e) from e

    def _restart_and_reconnect(self) -> None:
        try:
            self._uds_client.restart()
        except UdsOperationError as e:
            raise ProvisioningError("ER-006", ProvisioningStage.FAILED, f"Restart failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.RESTARTING)

    def _verify_integrity(self) -> None:
        try:
            self._uds_client.verify_certificate_integrity()
        except UdsOperationError as e:
            # ER-006 vs ER-007 ambiguity — see module docstring point 2.
            raise ProvisioningError("ER-006", ProvisioningStage.FAILED, f"Reconnect or integrity check failed: {e}", cause=e) from e
        self._emit(ProvisioningStage.RECONNECTED)
        self._emit(ProvisioningStage.INTEGRITY_VERIFIED)

    def _disconnect(self) -> None:
        try:
            self._uds_client.disconnect()
        except UdsOperationError as e:
            # No ER-00x maps to disconnect failure — FR-039/040 has no
            # matching entry in the doc's error table (only 7 ERs exist,
            # this isn't one of them). Treated as non-fatal: the actual
            # provisioning work already succeeded by this point, and
            # aborting a successful run over a cleanup failure seems worse
            # than logging it. Genuine judgment call, not spec-derived.
            logger.warning("Disconnect failed after successful provisioning: %s", e)
        self._emit(ProvisioningStage.DISCONNECTED)

    def _emit(self, stage: ProvisioningStage, detail: str | None = None) -> None:
        event = ProvisioningEvent(stage=stage, vin=self._current_vin, timestamp=_dt.datetime.now(), detail=detail)
        for reporter in self._reporters:
            reporter.report(event)