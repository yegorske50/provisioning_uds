"""
uds/codecs.py — StringCodec: one udsoncan.DidCodec class, fixed or variable
length depending on the `length` constructor argument.

Fixed (length=17 / 36): VIN, device_id, vin_alias — ISO 3779 / UUID-string width.
Variable (length=None): CSR, device certificate, root CA — PEM strings, hundreds
to thousands of bytes. __len__ raises DidCodec.ReadAllRemainingData for these —
verified against udsoncan 1.26.1's actual DidCodec base class source, which
requires exactly this to signal "read whatever ISO-TP framing delivered".
"""

from __future__ import annotations

from udsoncan import DidCodec


class StringCodec(DidCodec):
    def __init__(self, length: int | None = None):
        super().__init__()
        self._length = length

    def encode(self, val: str) -> bytes:
        data = val.encode("ascii")
        if self._length is not None and len(data) != self._length:
            raise ValueError(
                f"Expected exactly {self._length} bytes, got {len(data)} for value {val!r}"
            )
        return data

    def decode(self, payload: bytes) -> str:
        return payload.decode("ascii", errors="replace")

    def __len__(self) -> int:
        if self._length is None:
            raise DidCodec.ReadAllRemainingData
        return self._length