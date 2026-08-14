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

import logging
import os
import time
from dataclasses import dataclass, field

import isotp
from udsoncan import Response

from config import AppConfig, Did, RoutineId, load_config
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

# All six DIDs are opaque bytes — no field gets special treatment for being
# "commonly a string" (VIN, device_id, vin_alias) vs. "commonly binary" (CSR,
# device certificate, root CA). The cloud response is the source of truth for
# content and length; the simulator stores and returns exactly the bytes it
# receives, same contract as RawBytesCodec in uds/codecs.py, because a real
# ECU would do the same. No hardcoded length (was 17 for VIN, 36 for
# device_id/vin_alias) — that assumption is gone; the actual ECU spec never
# defined one (source doc's DID section is still blank).
_WRITABLE_DIDS = {Did.VIN, Did.DEVICE_ID, Did.VIN_ALIAS, Did.DEVICE_CERTIFICATE, Did.ROOT_CA}
# Did.CSR is deliberately excluded — it's server-generated (FR-007) and
# read-only. Previously a write to it silently no-op'd but still returned a
# positive response, since nothing in the old if/elif chain matched it; fixed
# here by making it explicitly rejected rather than silently accepted.


class _NegativeResponse(Exception):
    """Internal control flow only — carries the NRC to send back to the
    caller. Never escapes _handle_request()."""
    def __init__(self, code: int):
        self.code = code


@dataclass
class SimulatedEcuState:
    session: int = SESSION_DEFAULT
    last_activity: float = field(default_factory=time.monotonic)
    vin: bytes | None = None
    csr: bytes | None = None
    device_id: bytes | None = None
    vin_alias: bytes | None = None
    device_certificate: bytes | None = None
    root_ca: bytes | None = None
    reset_count: int = 0


def _dummy_binary_blob(size: int = 900) -> bytes:
    """Not shaped like any format — no PEM wrapper, no base64, no ASCII
    assumption. A real CSR is opaque bytes to this application, so the dummy
    one is too. Deterministically cycles through every byte value 0x00-0xFF,
    so tests can assert exact byte-for-byte preservation including the edge
    values (0x00, 0xFF, 0x80, etc.) that an accidental ASCII round-trip would
    corrupt or silently hide — this is exactly what _fake_pem's base64 output
    never exercised, since base64 is always ASCII-safe by construction."""
    full_range = bytes(range(256))
    repeated = full_range * (size // 256 + 1)
    return repeated[:size]


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
        return bytes([0x50, requested, 0x01, 0xF4, 0x01, 0xF4])

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
            value = self.state.csr  # already raw bytes — no transformation
        else:
            raise _NegativeResponse(Response.Code.RequestOutOfRange)

        return bytes([0x62]) + payload[1:3] + value

    # --- 0x2E WriteDataByIdentifier ---------------------------------------------
    def _handle_write_data_by_identifier(self, payload: bytes) -> bytes:
        self._require_extended_session()
        if len(payload) < 3:
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)
        did = int.from_bytes(payload[1:3], "big")
        # isotp's stack.recv() hands back a bytearray, not bytes — normalize
        # here, at the point raw wire data enters application code. This is
        # NOT a content transformation (the byte values are identical either
        # way — bytearray == bytes compares equal); it's a container-type fix
        # so server.state fields actually match the bytes contract.
        data = bytes(payload[3:])

        if did not in _WRITABLE_DIDS:
            # covers both genuinely-unknown DIDs and Did.CSR (read-only) —
            # same NRC either way, from this service's point of view neither
            # is a valid write target
            raise _NegativeResponse(Response.Code.RequestOutOfRange)
        if len(data) == 0:
            # a zero-byte payload is a degenerate/malformed request regardless
            # of which DID — this is a protocol-level sanity check, not a
            # content-format assumption about any particular field
            raise _NegativeResponse(Response.Code.IncorrectMessageLengthOrInvalidFormat)

        # every writable DID gets the exact bytes received, unchanged — no
        # decode, no length check, no format assumption for any of them
        if did == Did.VIN:
            self.state.vin = data
            self.state.csr = _dummy_binary_blob()  # FR-007: generated on VIN write
        elif did == Did.DEVICE_ID:
            self.state.device_id = data
        elif did == Did.VIN_ALIAS:
            self.state.vin_alias = data
        elif did == Did.DEVICE_CERTIFICATE:
            self.state.device_certificate = data
        elif did == Did.ROOT_CA:
            self.state.root_ca = data

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


def main() -> None:
    """Standalone entry point — runs the simulator as its own persistent
    process, listening on whatever config.load_config(env) resolves to, so
    main.py can be pointed at it from a separate terminal. Previously
    SimulatedEcuServer only ever ran inside test fixtures, in a background
    thread within the same pytest process — there was no way to start it on
    its own for a manual trial run."""
    from dotenv import load_dotenv  # local: only needed for the standalone runner, not when imported as a library

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    env = os.environ.get("PROVISIONING_ENV", "dev")
    cfg = load_config(env=env)
    stack = build_isotp_stack(cfg, role="ecu")
    server = SimulatedEcuServer(stack)
    print(f"Simulator listening on {cfg.can_bus.interface}:{cfg.can_bus.channel} (env={env}) — Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()