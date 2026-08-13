"""
uds/codecs.py — RawBytesCodec: the only codec this application uses.

Every DID (VIN, CSR, device_id, vin_alias, device certificate, root CA) is
opaque bytes now — the cloud response is the source of truth for content and
length, and the application does not encode, decode, pad, truncate, or
otherwise reformat any of it. encode/decode are pure identity.

StringCodec (fixed-length ASCII, used previously for VIN/device_id/vin_alias)
was removed. It existed because those fields are "commonly represented as
strings" — exactly the assumption the cloud-data requirement rules out: no
field gets special treatment just because it looks string-shaped, and no
field gets a hardcoded length (17 for VIN, 36 for device_id/vin_alias) unless
the actual ECU spec requires it — it doesn't; that section of the source doc
is still blank. If the ECU spec later fixes a real length constraint,
validate it explicitly at the point data enters the system and raise on
mismatch — never pad or truncate to fit.

A codec is still mandatory here even though it does nothing: udsoncan's
check_did_config() raises ConfigError for any DID with no entry in
data_identifiers and no 'default' fallback (verified directly against the
installed udsoncan 1.26.1 source — udsoncan/common/dids.py).
"""

from __future__ import annotations

from udsoncan import DidCodec


class RawBytesCodec(DidCodec):
    """encode/decode are pure identity — no encoding, no decoding, no format
    or length assumption beyond whatever ISO-TP framing actually delivered."""

    def __init__(self):
        super().__init__()

    def encode(self, value: bytes) -> bytes:
        return value

    def decode(self, payload: bytes) -> bytes:
        return payload

    def __len__(self) -> int:
        raise DidCodec.ReadAllRemainingData