"""
main.py — composition root.

Wires together config, transport, EcuUdsClient, CloudProvisioningClient,
reporters, and ProvisioningOrchestrator, then runs one provisioning cycle
against a VIN. Nothing above this file has ever needed to know about isotp,
can, requests, or dotenv — this is the one place all of that gets assembled.

VIN source: Open Question #7 (how the Dynomerk UI actually integrates) is
still unresolved, so VIN comes in as a CLI argument for now — the most
sensible placeholder for "we don't know the real integration yet", and a
one-line swap later (same process with a GUI, a separate LAN service calling
this as a library instead of a CLI, etc.) since nothing below this file
would need to change either way.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from config import load_config
from uds.transport import build_isotp_stack, shutdown
from uds.client import EcuUdsClient
from cloud import CloudProvisioningClient
from orchestration.workflow import ProvisioningOrchestrator
from orchestration.errors import ProvisioningError
from reporting import ConsoleStatusReporter


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ECx00 offline provisioning tool")
    parser.add_argument("--vin", required=True, help="VIN scanned/entered for the device being provisioned")
    parser.add_argument(
        "--env",
        default=os.environ.get("PROVISIONING_ENV", "dev"),
        choices=["dev", "prod"],
        help="Which config profile to use (default: $PROVISIONING_ENV or 'dev')",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()  # reads .env before load_config() runs — harmless if the file doesn't exist

    args = _parse_args(argv)
    cfg = load_config(env=args.env)

    stack = build_isotp_stack(cfg, role="client")  # <-- the one line that differs sim vs real hardware
    uds_client = EcuUdsClient(stack, cfg)
    cloud_client = CloudProvisioningClient(cfg.cloud_api)
    reporters = [ConsoleStatusReporter()]  # add a Dynomerk reporter once Open Question #7 is resolved
    orchestrator = ProvisioningOrchestrator(uds_client, cloud_client, reporters, model_code=cfg.model_code)

    try:
        orchestrator.run(args.vin)
    except ProvisioningError as e:
        print(f"Provisioning FAILED [{e.er_code}]: {e.message}", file=sys.stderr)
        return 1
    finally:
        shutdown(stack)  # transport is this file's to close — it's the one thing main.py built directly

    print("Provisioning succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())