"""
tests/unit/test_orchestrator.py

ProvisioningOrchestrator against hand-written fakes for UdsClientProtocol/
CloudClientProtocol — no bus, no network. This is where the state machine
and every ER-00x failure path actually get validated; the integration test
proves the real stack works, this proves the LOGIC is correct.
"""
from __future__ import annotations

import pytest

from cloud import Credentials, CloudApiError
from orchestration.errors import ProvisioningError
from orchestration.states import ProvisioningStage, ProvisioningEvent
from orchestration.workflow import ProvisioningOrchestrator
from uds.client import UdsOperationError


class _FakeUdsClient:
    """fail_at names a method that should raise UdsOperationError instead of
    succeeding — every other method records its call and succeeds."""

    def __init__(self, fail_at: str | None = None):
        self._fail_at = fail_at
        self.calls: list[tuple] = []

    def _record(self, name: str, *args):
        self.calls.append((name, *args))
        if self._fail_at == name:
            raise UdsOperationError(f"simulated failure at {name}")

    def connect(self) -> None: self._record("connect")
    def disconnect(self) -> None: self._record("disconnect")
    def write_vin(self, vin: bytes) -> None: self._record("write_vin", vin)

    def read_csr(self) -> bytes:
        self._record("read_csr")
        return b"fake-csr-bytes"

    def write_device_id(self, device_id: bytes) -> None: self._record("write_device_id", device_id)
    def write_vin_alias(self, alias: bytes) -> None: self._record("write_vin_alias", alias)
    def write_device_certificate(self, data: bytes) -> None: self._record("write_device_certificate", data)
    def write_root_ca(self, data: bytes) -> None: self._record("write_root_ca", data)
    def restart(self, wait_s: float = 2.0) -> None: self._record("restart")
    def verify_certificate_integrity(self) -> None: self._record("verify_certificate_integrity")


class _FakeCloudClient:
    def __init__(self, fail: bool = False, already_provisioned: bool = False):
        self._fail = fail
        self._already_provisioned = already_provisioned
        self.calls: list[tuple] = []

    def provision(self, vin: str, model_code: str, csr: bytes) -> Credentials:
        self.calls.append(("provision", vin, model_code, csr))
        if self._fail:
            raise CloudApiError("simulated cloud failure")
        if self._already_provisioned:
            return Credentials(
                message="device already provisioned",
                already_provisioned=True,
                device_id=b"fake-device-id",
                vin_alias=b"fake-vin-alias",
                device_certificate=None,
                root_ca=None,
            )
        return Credentials(
            message="device provisioned successfully",
            already_provisioned=False,
            device_id=b"fake-device-id",
            vin_alias=b"fake-vin-alias",
            device_certificate=b"fake-cert",
            root_ca=b"fake-root-ca",
        )


class _RecordingReporter:
    def __init__(self):
        self.events: list[ProvisioningEvent] = []

    def report(self, event: ProvisioningEvent) -> None:
        self.events.append(event)


def _orchestrator(uds=None, cloud=None, reporters=None):
    return ProvisioningOrchestrator(
        uds or _FakeUdsClient(),
        cloud or _FakeCloudClient(),
        reporters if reporters is not None else [_RecordingReporter()],
        model_code="ECX00-V1",
    )


def test_happy_path_returns_credentials_and_emits_success():
    uds = _FakeUdsClient()
    reporter = _RecordingReporter()
    orch = _orchestrator(uds=uds, reporters=[reporter])

    creds = orch.run("ABC12345678901234")

    assert creds.message == "device provisioned successfully"
    assert reporter.events[0].stage == ProvisioningStage.WAITING_FOR_DEVICE
    assert reporter.events[-1].stage == ProvisioningStage.SUCCESS
    assert all(e.stage != ProvisioningStage.FAILED for e in reporter.events)

    # VIN was UTF-8 encoded before reaching the UDS layer
    write_vin_call = next(c for c in uds.calls if c[0] == "write_vin")
    assert write_vin_call[1] == b"ABC12345678901234"

    # Credentials' bytes fields passed straight through, unmodified
    write_device_id_call = next(c for c in uds.calls if c[0] == "write_device_id")
    assert write_device_id_call[1] == b"fake-device-id"


@pytest.mark.parametrize("fail_at,expected_er_code", [
    ("connect", "ER-001"),
    ("write_vin", "ER-002"),
    ("read_csr", "ER-003"),
    ("write_device_id", "ER-005"),
    ("write_vin_alias", "ER-005"),
    ("write_device_certificate", "ER-005"),
    ("write_root_ca", "ER-005"),
    ("restart", "ER-006"),
    ("verify_certificate_integrity", "ER-006"),
])
def test_uds_failures_map_to_correct_er_code(fail_at, expected_er_code):
    uds = _FakeUdsClient(fail_at=fail_at)
    reporter = _RecordingReporter()
    orch = _orchestrator(uds=uds, reporters=[reporter])

    with pytest.raises(ProvisioningError) as exc_info:
        orch.run("ABC12345678901234")

    assert exc_info.value.er_code == expected_er_code
    assert reporter.events[-1].stage == ProvisioningStage.FAILED


def test_cloud_failure_maps_to_er_004():
    reporter = _RecordingReporter()
    orch = _orchestrator(cloud=_FakeCloudClient(fail=True), reporters=[reporter])

    with pytest.raises(ProvisioningError) as exc_info:
        orch.run("ABC12345678901234")

    assert exc_info.value.er_code == "ER-004"
    assert reporter.events[-1].stage == ProvisioningStage.FAILED


def test_already_provisioned_raises_without_guessing():
    uds = _FakeUdsClient()
    orch = _orchestrator(uds=uds, cloud=_FakeCloudClient(already_provisioned=True))

    with pytest.raises(ProvisioningError) as exc_info:
        orch.run("ABC12345678901234")

    assert exc_info.value.er_code == "ER-004"
    # never attempted to program credentials it didn't have
    assert not any(c[0] == "write_device_id" for c in uds.calls)


def test_disconnect_failure_is_non_fatal():
    """No ER-00x maps to disconnect failure — a genuine judgment call this
    orchestrator makes: log it, don't fail an otherwise-successful run."""
    reporter = _RecordingReporter()
    orch = _orchestrator(uds=_FakeUdsClient(fail_at="disconnect"), reporters=[reporter])

    creds = orch.run("ABC12345678901234")  # does NOT raise

    assert creds.message == "device provisioned successfully"
    stages = [e.stage for e in reporter.events]
    assert ProvisioningStage.DISCONNECTED in stages
    assert stages[-1] == ProvisioningStage.SUCCESS  # SUCCESS is the true terminal event, after DISCONNECTED


def test_multiple_reporters_all_receive_events():
    r1, r2 = _RecordingReporter(), _RecordingReporter()
    orch = _orchestrator(reporters=[r1, r2])

    orch.run("ABC12345678901234")

    assert len(r1.events) == len(r2.events) > 0
    assert r1.events[-1].stage == r2.events[-1].stage == ProvisioningStage.SUCCESS