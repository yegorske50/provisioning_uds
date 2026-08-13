"""
uds/client.py — EcuUdsClient: domain-specific facade over udsoncan.Client.

Behavior is identical whether `stack` came from the simulator or real
hardware — that difference lives entirely in uds/transport.py, never here.

Session handling: every write-sensitive method re-enters extended session
(10 03) defensively before doing anything, rather than trusting the session
entered at connect() is still alive. This is the fix agreed on in place of a
background TesterPresent thread — it covers the one genuinely unbounded wait
in the whole flow (the cloud API call) without needing a thread at all, and
it's verified for real in tests/integration/test_simulator_transport.py
(session-timeout test) and tests/integration/test_ecu_client.py.

One consequence worth knowing: because every method already re-enters the
session defensively, "reconnect after restart" (FR-032/033) doesn't need its
own method — the next call after restart() just re-enters the session as
normal. See ProvisioningOrchestrator._restart_and_reconnect() when that's built.
"""

from __future__ import annotations

import time

import isotp
import udsoncan
from udsoncan.client import Client
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.exceptions import (
    InvalidResponseException,
    NegativeResponseException,
    TimeoutException,
    UnexpectedResponseException,
)
from udsoncan.services import DiagnosticSessionControl, ECUReset, RoutineControl

from config import AppConfig, Did, RoutineId
from uds.codecs import RawBytesCodec

# Operational failures from udsoncan get translated to this — nothing above
# this class needs to know udsoncan exists, matching every other adapter
# boundary in this codebase (CloudApiError, ProvisioningError, etc.)
_UDS_EXCEPTIONS = (
    NegativeResponseException,
    TimeoutException,
    InvalidResponseException,
    UnexpectedResponseException,
)


class UdsOperationError(Exception):
    pass


_DID_CONFIG = {
    Did.VIN: RawBytesCodec(),                  # opaque bytes — no length/format assumed
    Did.CSR: RawBytesCodec(),                  # opaque bytes — no format assumed
    Did.DEVICE_ID: RawBytesCodec(),            # opaque bytes — no length/format assumed
    Did.VIN_ALIAS: RawBytesCodec(),            # opaque bytes — no length/format assumed
    Did.DEVICE_CERTIFICATE: RawBytesCodec(),   # opaque bytes — no format assumed
    Did.ROOT_CA: RawBytesCodec(),              # opaque bytes — no format assumed
}


class EcuUdsClient:
    def __init__(self, stack: isotp.CanStack, cfg: AppConfig):
        conn = PythonIsoTpConnection(stack)
        config = dict(udsoncan.configs.default_client_config)
        config["data_identifiers"] = _DID_CONFIG
        self._client = Client(conn, config=config, request_timeout=cfg.request_timeout_s)

    # --- lifecycle -----------------------------------------------------------
    def connect(self) -> None:
        """FR-002..004."""
        self._run(self._client.open)
        self._enter_extended_session()

    def disconnect(self) -> None:
        """FR-039..040. Explicit return to default session before closing —
        this was the whole point of the earlier TesterPresent discussion:
        don't just walk away from extended session."""
        self._run(self._client.change_session, DiagnosticSessionControl.Session.defaultSession)
        self._run(self._client.close)

    def _enter_extended_session(self) -> None:
        self._run(self._client.change_session, DiagnosticSessionControl.Session.extendedDiagnosticSession)

    # --- FR-mapped operations --------------------------------------------------
    def write_vin(self, vin: bytes) -> None:
        """FR-005..008. `vin` is opaque bytes — exactly what the caller
        (eventually the cloud response) provides, no length assumption
        (was 17 — removed, unsupported by the actual ECU spec, which is
        still blank on this), no ASCII/format transformation. See
        uds/codecs.py RawBytesCodec."""
        self._enter_extended_session()
        self._run(self._client.write_data_by_identifier, Did.VIN, vin)

    def read_csr(self) -> bytes:
        """FR-009..013. Returns opaque bytes exactly as the ECU sent them —
        no ASCII/PEM/base64 assumption. See uds/codecs.py RawBytesCodec."""
        self._enter_extended_session()
        response = self._run(self._client.read_data_by_identifier, [Did.CSR])
        return response.service_data.values[Did.CSR]

    def write_device_id(self, device_id: bytes) -> None:
        """FR-018..021. Opaque bytes, no length assumption (was 36) — same
        reasoning as write_vin."""
        self._enter_extended_session()
        self._run(self._client.write_data_by_identifier, Did.DEVICE_ID, device_id)

    def write_vin_alias(self, alias: bytes) -> None:
        """FR-022..025. Opaque bytes, no length assumption — same reasoning
        as write_vin."""
        self._enter_extended_session()
        self._run(self._client.write_data_by_identifier, Did.VIN_ALIAS, alias)

    def write_device_certificate(self, data: bytes) -> None:
        """FR-026..029. `data` is opaque bytes, passed through unchanged —
        no ASCII/PEM/base64 assumption. See uds/codecs.py RawBytesCodec."""
        self._enter_extended_session()
        self._run(self._client.write_data_by_identifier, Did.DEVICE_CERTIFICATE, data)

    def write_root_ca(self, data: bytes) -> None:
        """Not in the FR list explicitly, but the cloud response returns one —
        see architecture doc Open Question #6. `data` is opaque bytes, same as
        write_device_certificate."""
        self._enter_extended_session()
        self._run(self._client.write_data_by_identifier, Did.ROOT_CA, data)

    def restart(self, wait_s: float = 2.0) -> None:
        """FR-030..031. Real hardware drops off the bus briefly after a hard
        reset — wait_s covers that gap. The next call after this one
        re-enters extended session defensively, which is FR-032/033's
        "reconnect" — no separate method needed."""
        self._enter_extended_session()
        self._run(self._client.ecu_reset, ECUReset.ResetType.hardReset)
        time.sleep(wait_s)

    def verify_certificate_integrity(self) -> None:
        """FR-035..038. Raises UdsOperationError on failure — doesn't return
        a bool, because udsoncan's default config already raises
        NegativeResponseException on a negative response, so a returned
        False would never actually happen; a bool return here would be a
        contract that lies about when it can be false."""
        self._enter_extended_session()
        self._run(
            self._client.routine_control,
            RoutineId.VERIFY_CERTIFICATE_INTEGRITY,
            RoutineControl.ControlType.startRoutine,
        )

    # --- internal --------------------------------------------------------------
    def _run(self, fn, *args):
        try:
            return fn(*args)
        except _UDS_EXCEPTIONS as e:
            raise UdsOperationError(str(e)) from e