"""
tests/integration/test_main.py

Proves main.py's composition root actually wires everything correctly and
runs end to end — driven through main() itself rather than constructing the
pieces by hand like test_orchestrator_end_to_end.py does.

Uses PROVISIONING_DEV_CAN_PORT to isolate onto its own port (43204) rather
than the real dev default (43113). Originally this used the literal dev
default on purpose, to prove main.py's actual default wiring worked — but
that meant this test would collide with a manually-run `python simulator.py
--env dev` on the same machine, since both bind the same udp_multicast
port/group. That's exactly what happened in practice: crossed responses
between the test's own simulator and a leftover manual one, surfacing as an
UnexpectedResponseException that had nothing to do with main.py's logic.
Isolating the port removes that entire class of collision; addressing,
cloud config, and everything else still comes from the real load_config("dev").
"""
from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock

import pytest

from config import load_config
from uds.transport import build_isotp_stack, shutdown
from simulator import SimulatedEcuServer
import main as main_module


def _mock_cloud_response(vin: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "message": "device provisioned successfully",
        "vin": vin,
        "vin_alias": "1f18bf10-ef10-60c4-b817-9221ddc76d4b",
        "vehicle_model_id": 2,
        "device_id": "1f18bf10-f7b6-6131-b818-9221ddc76d4b",
        "certificate_id": "ccf5b31efdbaeec",
        "device_cert": "-----BEGIN CERTIFICATE-----\ndummycertbytes\n-----END CERTIFICATE-----\n",
        "aws_root_ca": "-----BEGIN CERTIFICATE-----\ndummyrootcabytes\n-----END CERTIFICATE-----\n",
    }
    return resp


@pytest.fixture
def running_dev_simulator(monkeypatch):
    # Set before load_config("dev") runs — both this fixture's own call AND
    # main_module.main()'s internal load_config("dev") call (invoked later,
    # when the test calls main()) read this from the same process env, so
    # both land on the isolated port automatically.
    monkeypatch.setenv("PROVISIONING_DEV_CAN_PORT", "43204")
    monkeypatch.setenv("PROVISIONING_API_KEY", "dummy-key-for-test")

    cfg = load_config("dev")
    ecu_stack = build_isotp_stack(cfg, role="ecu")
    server = SimulatedEcuServer(ecu_stack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server

    server.stop()
    shutdown(ecu_stack)


def test_main_runs_successfully_end_to_end(running_dev_simulator):
    server = running_dev_simulator
    vin = "ABC12345678901234"

    with patch("cloud.requests.post") as mock_post:
        mock_post.return_value = _mock_cloud_response(vin)
        exit_code = main_module.main(["--vin", vin, "--env", "dev"])

    assert exit_code == 0
    assert server.state.vin == vin.encode("utf-8")
    assert server.state.device_id == b"1f18bf10-f7b6-6131-b818-9221ddc76d4b"
    assert server.state.reset_count == 1


def test_main_returns_nonzero_on_provisioning_failure(running_dev_simulator):
    vin = "ABC12345678901234"
    fail_response = MagicMock(status_code=500, ok=False, text="server error")

    with patch("cloud.requests.post") as mock_post, patch("cloud.time.sleep"):
        mock_post.return_value = fail_response  # every attempt fails — retries exhaust, then ER-004
        exit_code = main_module.main(["--vin", vin, "--env", "dev"])

    assert exit_code == 1


def test_main_requires_vin_argument(running_dev_simulator):
    with pytest.raises(SystemExit):
        main_module.main(["--env", "dev"])  # no --vin — argparse should reject this