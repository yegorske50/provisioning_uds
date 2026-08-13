"""
simulator.py — SimulatedEcuState + SimulatedEcuServer.

Real isotp.CanStack underneath (via uds.transport.build_isotp_stack(cfg,
role="ecu")). Only the business logic is fake: a handful of in-memory fields,
no persistence, no security access, no flash emulation.

Enforces SESSION_TIMEOUT_S: more than that many seconds idle while in extended
session and the session silently reverts to default — matching real ECU
behavior, and specifically here so the TesterPresent/session-drop scenario we
discussed gets caught in dev instead of only on real hardware.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field

import isotp
from udsoncan import Response

from config import AppConfig, Did, RoutineId
from uds.transport import build_isotp_stack

logger = logging.getLogger(__name__)

# --- UDS service IDs this simulator understands ---------------------------
SID_DIAGNOSTIC_SESSION_CONTROL = 0x10
SID_ECU_RESET = 0x11
SID_READ_DATA_BY_IDENTIFIER = 0x22
SID_WRITE_DATA_BY_IDENTIFIER = 0x2E
SID_ROUTINE_CONTROL = 0x31

SESSION_DEFAULT = 0x01
SESSION_EXTENDED = 0x03

_FIXED_LENGTH_DIDS = {
    Did.VIN: 17,
    Did.DEVICE_ID: 36,
    Did.VIN_ALIAS: 36,
}
_VARIABLE_LENGTH_DIDS = {Did.CSR, Did.DEVICE_CERTIFICATE, Did.ROOT_CA}


class _NegativeResponse(Exception):
    """Internal control flow only — carries the NRC to send back to the
    caller. Never escapes _handle_request()."""
    def __init__(self, code: int):
        self.code = code


@dataclass
class SimulatedEcuState:
    session: int = SESSION_DEFAULT
    last_activity: float = field(default_factory=time.monotonic)
    vin: str | None = None
    csr: str | None = None
    device_id: str | None = None
    vin_alias: str | None = None
    device_certificate: str | None = None
    root_ca: str | None = None
    reset_count: int = 0


def _fake_pem(label: str, body_bytes: int = 900) -> str:
    """Not a real CSR/cert — just realistically sized (well over one CAN
    frame) so ISO-TP multi-frame transfer actually gets exercised in dev,
    instead of only discovering it works on real hardware."""
    raw = base64.b64encode(os.urandom(body_bytes)).decode("ascii")
    wrapped = "\n".join(raw[i:i + 64] for i in range(0, len(raw), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


class SimulatedEcuServer:
    SESSION_TIMEOUT_S = 5.0

    def __init__(self, stack: isotp.CanStack):
        self.stack = stack
        self.state = SimulatedEcuState()
        self._running = False

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "SimulatedEcuServer":
        return cls(build_isotp_stack(cfg, role="ecu"))

    def serve_forever(self, poll_interval: float = 0.02) -> None:
        logger.info("SimulatedEcuServer listening...")
        self._running = True
        while self._running:
            self._check_session_timeout()
            payload = self.stack.recv()
            if payload is None:
                time.sleep(poll_interval)
                continue
            self.state.last_activity = time.monotonic()
            response = self._handle_request(payload)
            if response is not None:
                self.stack.send(response)

    def stop(self) -> None:
        self._running = False

    def _check_session_timeout(self) -> None:
        if self.state.session != SESSION_DEFAULT:
            idle = time.monotonic() - self.state.last_activity
            if idle > self.SESSION_TIMEOUT_S:
                logger.warning(
                    "Session idle %.1fs > %.1fs — reverting to default session",
                    idle, self.SESSION_TIMEOUT_S,
                )
                self.state.session = SESSION_DEFAULT

    def _handle_request(self, payload: bytes) -> bytes | None:
        if not payload:
            return None
        sid = payload[0]
        try:
            if sid == SID_DIAGNOSTIC_SESSION_CONTROL:
                return self._handle_session_control(payload)
            if sid == SID_ECU_RESET:
                return self._handle_ecu_reset(payload)
            if sid == SID_READ_DATA_BY_IDENTIFIER:
                return self._handle_read_data_by_identifier(payload)
            if sid == SID_WRITE_DATA_BY_IDENTIFIER:
                return self._handle_write_data_by_identifier(payload)
            if sid == SID_ROUTINE_CONTROL:
                return self._handle_routine_control(payload)
            raise _NegativeResponse(Response.Code.ServiceNotSupported)
        except _NegativeResponse as nrc:
            logger.info("NRC 0x%02X for SID 0x%02X", nrc.code, sid)
            return bytes([0x7F, sid, nrc.code])

    def _require_extended_session(self) -> None:
        if self.state.session != SESSION_EXTENDED:
            raise _NegativeResponse(Response.Code.ServiceNotSupportedInActiveSession)

    # --- 0x10 DiagnosticSessionControl -------------------------------------
    def _handle_session_control(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        requested = payload[1]
        if requested not in (SESSION_DEFAULT, SESSION_EXTENDED):
            raise _NegativeResponse(Response.Code.SubFunctionNotSupported)
        self.state.session = requested
        # P2=500ms, P2*=5000ms. Not just decorative: udsoncan's default config has
        # use_server_timing=True, so it adopts THESE values as the timeout for every
        # subsequent request, overriding whatever request_timeout the client was built
        # with. An unrealistically tight P2 here (e.g. the ISO default of 50ms) makes
        # udsoncan impose that same 50ms on itself — easily blown by ordinary Python
        # thread-scheduling jitter, as a real failing test run demonstrated.
        return bytes([0x50, requested, 0x01, 0xF4, 0x13, 0x88])

    # --- 0x11 ECUReset -------------------------------------------------------
    def _handle_ecu_reset(self, payload: bytes) -> bytes:
        self._require_extended_session()
        if len(payload) < 2:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        reset_type = payload[1]
        self.state.reset_count += 1
        self.state.session = SESSION_DEFAULT
        self.state.last_activity = time.monotonic()
        return bytes([0x51, reset_type])

    # --- 0x22 ReadDataByIdentifier ---------------------------------------------
    def _handle_read_data_by_identifier(self, payload: bytes) -> bytes:
        self._require_extended_session()
        if len(payload) < 3:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        did = int.from_bytes(payload[1:3], "big")

        if did == Did.CSR:
            if self.state.csr is None:
                raise _NegativeResponse(Response.Code.ConditionsNotCorrect)  # write VIN first
            value = self.state.csr.encode("ascii")
        else:
            raise _NegativeResponse(Response.Code.RequestOutOfRange)

        return bytes([0x62]) + payload[1:3] + value

    # --- 0x2E WriteDataByIdentifier ---------------------------------------------
    def _handle_write_data_by_identifier(self, payload: bytes) -> bytes:
        self._require_extended_session()
        if len(payload) < 3:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        did = int.from_bytes(payload[1:3], "big")
        data = payload[3:]

        if did in _FIXED_LENGTH_DIDS:
            expected_len = _FIXED_LENGTH_DIDS[did]
            if len(data) != expected_len:
                raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
            value = data.decode("ascii", errors="replace")
        elif did in _VARIABLE_LENGTH_DIDS:
            if len(data) == 0:
                raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
            value = data.decode("ascii", errors="replace")
        else:
            raise _NegativeResponse(Response.Code.RequestOutOfRange)

        if did == Did.VIN:
            self.state.vin = value
            self.state.csr = _fake_pem("CERTIFICATE REQUEST")  # FR-007: generated on VIN write
        elif did == Did.DEVICE_ID:
            self.state.device_id = value
        elif did == Did.VIN_ALIAS:
            self.state.vin_alias = value
        elif did == Did.DEVICE_CERTIFICATE:
            self.state.device_certificate = value
        elif did == Did.ROOT_CA:
            self.state.root_ca = value

        return bytes([0x6E]) + payload[1:3]

    # --- 0x31 RoutineControl ----------------------------------------------------
    def _handle_routine_control(self, payload: bytes) -> bytes:
        self._require_extended_session()
        if len(payload) < 4:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        control_type = payload[1]
        routine_id = int.from_bytes(payload[2:4], "big")

        if routine_id != RoutineId.VERIFY_CERTIFICATE_INTEGRITY:
            raise _NegativeResponse(Response.Code.RequestOutOfRange)

        have_everything = all([
            self.state.device_id,
            self.state.vin_alias,
            self.state.device_certificate,
            self.state.root_ca,
        ])
        if not have_everything:
            raise _NegativeResponse(Response.Code.ConditionsNotCorrect)

        return bytes([0x71]) + payload[1:4]