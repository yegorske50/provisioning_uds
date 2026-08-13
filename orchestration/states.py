"""
orchestration/states.py — ProvisioningStage enum + ProvisioningEvent.

Stages map onto the FRs that say "notify the Dynomerk UI" — one per FR group
in ProvisioningOrchestrator.run().
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ProvisioningStage(str, Enum):
    WAITING_FOR_DEVICE = "Waiting For Device"
    DEVICE_CONNECTED = "Device Connected"
    VIN_PROGRAMMED = "VIN Programmed"
    CSR_RECEIVED = "CSR Received"
    CALLING_CLOUD_API = "Calling Cloud API"
    CREDENTIALS_RECEIVED = "Credentials Received"
    ALREADY_PROVISIONED = "Already Provisioned"     # Open Question #3 — no certs to program in this branch
    DEVICE_ID_PROGRAMMED = "Device ID Programmed"
    VIN_ALIAS_PROGRAMMED = "VIN Alias Programmed"
    CERTIFICATES_PROGRAMMED = "Certificates Programmed"
    RESTARTING = "Device Restart In Progress"
    RECONNECTED = "Device Reconnected"
    INTEGRITY_VERIFIED = "Certificate Integrity Verified"
    DISCONNECTED = "Device Disconnected"
    SUCCESS = "Provisioning Successful"
    FAILED = "Provisioning Failed"


@dataclass
class ProvisioningEvent:
    stage: ProvisioningStage
    vin: str
    timestamp: datetime
    detail: str | None = None