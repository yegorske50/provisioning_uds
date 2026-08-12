"""
config.py — environment-driven settings for the ECx00 offline provisioning tool.

This is the ONLY place that decides:
  - which CAN bus to open (dev simulator vs. real hardware)
  - which UDS addressing scheme to use
  - where the cloud API lives and how to authenticate to it

Everything else in this codebase takes an AppConfig instance and never reads
os.environ directly. See ECx00_Provisioning_Tool_Architecture.md section 3.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum


# ---------------------------------------------------------------------------
# Bus / transport settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanBusConfig:
    """python-can bus parameters. `interface` decides everything: 'udp_multicast'
    for the local simulator, 'socketcan' / 'vector' / 'pcan' etc. for real
    hardware. This is the one field transport.py reads to build the actual bus.
    """
    interface: str
    channel: str
    port: int | None = None          # udp_multicast only
    bitrate: int | None = None       # required once this points at real hardware


@dataclass(frozen=True)
class UdsAddressConfig:
    """isotp addressing. rxid/txid and addressing_mode are placeholders —
    confirm with firmware before pointing this at real ECx00 hardware
    (architecture doc, Open Question #1)."""
    addressing_mode: str   # "normal_11bits" for now
    rxid: int
    txid: int


# ---------------------------------------------------------------------------
# Cloud API settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloudApiConfig:
    provision_url: str
    reprovision_url: str
    api_key: str            # always from env — see load_config(), never hardcoded
    timeout_s: float = 5.0
    max_retries: int = 3


# ---------------------------------------------------------------------------
# App-wide settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    can_bus: CanBusConfig
    uds_address: UdsAddressConfig
    cloud_api: CloudApiConfig
    request_timeout_s: float = 2.0
    model_code: str = ""    # source undefined in the FRs — Open Question #5


def load_config(env: str = "dev") -> AppConfig:
    """
    env="dev"  -> udp_multicast simulator bus, dev cloud URLs, empty API key OK.
    env="prod" -> real CAN adapter, prod cloud URLs, everything required from env
                  — raises immediately if anything's missing rather than limping
                  along with a silently wrong default.

    The API key always comes from PROVISIONING_API_KEY. Never hardcode it here,
    not even a dev placeholder — set it in your shell or a .env file instead.
    """
    api_key = os.environ.get("PROVISIONING_API_KEY", "")

    if env == "dev":
        return AppConfig(
            can_bus=CanBusConfig(
                interface="udp_multicast",
                channel="239.0.0.1",
                port=43113,
            ),
            uds_address=UdsAddressConfig(
                addressing_mode="normal_11bits",
                rxid=0x7E8,
                txid=0x7E0,
            ),
            cloud_api=CloudApiConfig(
                provision_url=os.environ.get(
                    "PROVISIONING_PROVISION_URL",
                    "https://api.dev.tmtx.bajajauto.com/v1/device/provisioning",
                ),
                reprovision_url=os.environ.get(
                    "PROVISIONING_REPROVISION_URL",
                    "https://api.dev.tmtx.bajajauto.com/v1/device/re-provisioning",
                ),
                api_key=api_key,
            ),
            model_code=os.environ.get("PROVISIONING_MODEL_CODE", "DEV-MODEL"),
        )

    if env == "prod":
        missing = [
            name for name in (
                "PROVISIONING_CAN_INTERFACE",
                "PROVISIONING_CAN_CHANNEL",
                "PROVISIONING_PROVISION_URL",
                "PROVISIONING_REPROVISION_URL",
                "PROVISIONING_API_KEY",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(
                "Refusing to start against prod — missing required env vars: "
                + ", ".join(missing)
            )
        return AppConfig(
            can_bus=CanBusConfig(
                interface=os.environ["PROVISIONING_CAN_INTERFACE"],   # e.g. "socketcan"
                channel=os.environ["PROVISIONING_CAN_CHANNEL"],        # e.g. "can0"
                bitrate=int(os.environ.get("PROVISIONING_CAN_BITRATE", "500000")),
            ),
            uds_address=UdsAddressConfig(
                addressing_mode="normal_11bits",   # CONFIRM with firmware before real use
                rxid=int(os.environ.get("PROVISIONING_UDS_RXID", "0x7E8"), 16),
                txid=int(os.environ.get("PROVISIONING_UDS_TXID", "0x7E0"), 16),
            ),
            cloud_api=CloudApiConfig(
                provision_url=os.environ["PROVISIONING_PROVISION_URL"],
                reprovision_url=os.environ["PROVISIONING_REPROVISION_URL"],
                api_key=api_key,
            ),
            model_code=os.environ.get("PROVISIONING_MODEL_CODE", ""),
        )

    raise ValueError(f"Unknown env: {env!r} — expected 'dev' or 'prod'")


# ---------------------------------------------------------------------------
# DID / routine contract — SINGLE SOURCE OF TRUTH.
#
# uds/client.py and simulator.py both import from here. Never redefine a DID
# inline anywhere else. Values below are placeholders — confirm the real ones
# with firmware before pointing this at actual hardware (architecture doc,
# Open Question #1). The doc's own "UDS and DID Details" section is blank,
# so these numbers carry no authority beyond "used consistently in dev."
# ---------------------------------------------------------------------------

class Did(IntEnum):
    VIN = 0xF190
    CSR = 0xF1A0
    DEVICE_ID = 0xF1A1
    VIN_ALIAS = 0xF1A2
    DEVICE_CERTIFICATE = 0xF1A3
    ROOT_CA = 0xF1A4


class RoutineId(IntEnum):
    VERIFY_CERTIFICATE_INTEGRITY = 0x0301
