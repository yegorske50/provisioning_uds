"""
uds/transport.py — the ONE module allowed to differ between dev and prod.

Builds the isotp.CanStack from AppConfig.can_bus / AppConfig.uds_address.
Nothing outside this file imports `can` or `isotp` directly.
"""

from __future__ import annotations

from typing import Literal

import can
import isotp

from config import AppConfig

_ADDRESSING_MODES = {
    "normal_11bits": isotp.AddressingMode.Normal_11bits,
}


def build_isotp_stack(cfg: AppConfig, *, role: Literal["client", "ecu"] = "client") -> isotp.CanStack:
    """
    Opens the python-can Bus per cfg.can_bus, wraps it in an isotp.Address per
    cfg.uds_address, returns the isotp.CanStack. This is the ONLY function in
    the codebase that imports `can`/`isotp` directly — everything above it
    only ever sees the returned stack.

    role="client" (default): rxid/txid used exactly as config.py defines them
    (the tester's perspective — how config.py's values are defined). Left
    UNSTARTED: udsoncan.Client's open()/`with Client(...)` starts the isotp
    layer itself, and isotp raises RuntimeError if start() is called twice
    (verified against isotp 2.0.7's TransportLayer.start() source) — so this
    function must not start it for the client role. If you're driving the
    stack directly without udsoncan.Client (e.g. a raw test), call
    stack.start() yourself after this returns.

    role="ecu": rxid/txid swapped, since the ECU listens on the tester's txid
    and replies on the tester's rxid. Started immediately — simulator.py
    drives the stack directly via send()/recv(), so nothing else will start it.
    """
    bus_kwargs: dict = {
        "interface": cfg.can_bus.interface,
        "channel": cfg.can_bus.channel,
    }
    if cfg.can_bus.port is not None:
        bus_kwargs["port"] = cfg.can_bus.port
    if cfg.can_bus.bitrate is not None:
        bus_kwargs["bitrate"] = cfg.can_bus.bitrate

    bus = can.interface.Bus(**bus_kwargs)

    try:
        mode = _ADDRESSING_MODES[cfg.uds_address.addressing_mode]
    except KeyError:
        raise ValueError(
            f"Unsupported addressing_mode: {cfg.uds_address.addressing_mode!r} "
            f"— supported: {list(_ADDRESSING_MODES)}"
        )

    rxid, txid = cfg.uds_address.rxid, cfg.uds_address.txid
    if role == "ecu":
        rxid, txid = txid, rxid

    address = isotp.Address(mode, rxid=rxid, txid=txid)
    stack = isotp.CanStack(bus, address=address)

    if role == "ecu":
        stack.start()

    return stack


def shutdown(stack: isotp.CanStack) -> None:
    """Stops the isotp layer (if running) and closes the underlying bus.
    stack.bus is set by isotp.CanStack itself — verified against isotp 2.0.7:
    CanStack.set_bus() does `self.bus = bus`."""
    if stack.started:
        stack.stop()
    stack.bus.shutdown()