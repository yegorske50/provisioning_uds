"""
orchestration/errors.py — ProvisioningError: one class, er_code field
(e.g. "ER-002"), not seven subclasses — every ER-00x is handled identically
by the doc ("terminate and report"), so there's no code path that needs to
tell them apart by exception type. See v2 architecture doc changelog.
"""
from __future__ import annotations

from orchestration.states import ProvisioningStage


class ProvisioningError(Exception):
    """er_code is the actual doc reference (e.g. "ER-002"), stage is where it
    happened, cause is the underlying exception if any (UdsOperationError,
    CloudApiError, ...) — chained via `from cause` at the raise site so the
    original traceback is never lost."""

    def __init__(self, er_code: str, stage: ProvisioningStage, message: str, cause: Exception | None = None):
        super().__init__(f"[{er_code}] {message}")
        self.er_code = er_code
        self.stage = stage
        self.message = message
        self.cause = cause