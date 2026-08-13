"""
reporting.py — StatusReporter protocol + ConsoleStatusReporter.

Exists because the doc never says how the Offline Provisioning Script
actually talks to the Dynomerk UI — same process, separate LAN service, log
file it tails, something else. The orchestrator only knows about
StatusReporter; when the real integration is confirmed, add one class
(DynomerkUiReporter) and wire it into main.py. Zero orchestrator changes.
"""
from __future__ import annotations

import logging
from typing import Protocol

from orchestration.states import ProvisioningEvent

logger = logging.getLogger(__name__)


class StatusReporter(Protocol):
    def report(self, event: ProvisioningEvent) -> None: ...


class ConsoleStatusReporter:
    """Prints/logs. Stays in production too, independent of whatever
    Dynomerk integration turns out to be — a plain trail regardless."""

    def report(self, event: ProvisioningEvent) -> None:
        detail = f" — {event.detail}" if event.detail else ""
        print(f"[{event.timestamp:%H:%M:%S}] {event.vin}: {event.stage.value}{detail}")
        logger.info("VIN=%s stage=%s detail=%s", event.vin, event.stage.value, event.detail)