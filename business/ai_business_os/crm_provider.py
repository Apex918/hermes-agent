"""CRM provider contract for AI Business OS.

CRM providers expose a side-effect-free read path and an approval-gated write
path. Reads return customer-record snapshots. Writes are first proposed so the
caller can review preview/diff/rollback metadata, then committed with an
approval receipt and an idempotency key so the provider can dedupe retries.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .approval_state_machine import ApprovalLevel, ApprovalStatus

DEFAULT_CRM_APPROVAL_LEVEL: ApprovalLevel = "R2"


@dataclass(frozen=True)
class CRMWriteProposal:
    """Immutable proposal for a CRM write.

    ``approval_level`` defaults to R2 because CRM writes are expected to require
    a human approval receipt before commit. ``receipt_required`` remains explicit
    so a caller can tell whether a given proposal must be approved before it can
    be applied.
    """

    operation: str
    payload: Dict[str, Any]
    summary: str
    origin: str
    approval_level: ApprovalLevel
    receipt_required: bool
    preview: str
    diff: str
    rollback: Dict[str, Any] = field(default_factory=dict)
    audit_ref: str = ""
    idempotency_key: str = ""
    status: ApprovalStatus = "pending"
    dry_run: bool = False
    reason: str = ""
    gate_decision: str = ""


@dataclass(frozen=True)
class CRMCommitResult:
    """Result envelope for a CRM commit attempt."""

    success: bool
    status: ApprovalStatus
    operation: str
    approval_level: ApprovalLevel
    approval_receipt: str
    idempotency_key: str
    preview: str
    diff: str
    rollback: Dict[str, Any]
    audit_ref: str
    result: Any = None
    error: str = ""
    idempotent: bool = False
    gate_decision: str = ""
    terminal_status: ApprovalStatus | None = None

    def __post_init__(self) -> None:
        """Keep the pre-N2 ``approved`` facade while exposing final state.

        Older provider consumers treated a successful commit as ``approved``.
        The shared state machine now distinguishes approval from application;
        retain that source-compatible label and expose the authoritative final
        state as ``terminal_status``.
        """
        if self.terminal_status is None:
            object.__setattr__(self, "terminal_status", self.status)
        if self.status == "applied":
            object.__setattr__(self, "status", "approved")


class CRMProvider(abc.ABC):
    """Abstract base class for a CRM backend.

    Subclasses must implement :meth:`name`, :meth:`read`, :meth:`propose_write`,
    and :meth:`commit`. The contract is intentionally simple:

    - ``read`` is side-effect free and may be called freely.
    - ``propose_write`` builds a proposal that includes audit / rollback /
      idempotency metadata.
    - ``commit`` requires both an approval receipt and the proposal's
      idempotency key so the provider can dedupe retries safely.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in config and logs."""

    @property
    def display_name(self) -> str:
        """Human-readable label shown in tools and diagnostics."""

        return self.name.replace("_", " ").title()

    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        The default is True so a fake or in-memory provider can opt in without
        any external credentials or SDK dependencies.
        """

        return True

    @abc.abstractmethod
    def read(
        self,
        query: str,
        *,
        limit: int = 20,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Read CRM records without mutating the backend."""

    @abc.abstractmethod
    def propose_write(
        self,
        operation: str,
        payload: Dict[str, Any],
        *,
        summary: str = "",
        origin: str = "foreground",
        dry_run: bool = False,
        approval_level: ApprovalLevel = DEFAULT_CRM_APPROVAL_LEVEL,
        **kwargs: Any,
    ) -> CRMWriteProposal:
        """Build an approval-gated write proposal.

        ``approval_level`` defaults to R2 so CRM mutations are receipt-gated by
        default. Implementations should keep the proposal pure: no side effects,
        no remote writes, just the metadata needed for review and replay.
        """

    @abc.abstractmethod
    def commit(
        self,
        proposal: CRMWriteProposal,
        *,
        approval_receipt: str,
        idempotency_key: str,
        **kwargs: Any,
    ) -> CRMCommitResult:
        """Apply an approved proposal once.

        ``approval_receipt`` and ``idempotency_key`` are required keyword-only
        inputs so callers cannot accidentally skip the approval or replay the
        same write twice under a different key.
        """

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return picker metadata for the contract provider.

        A fake/in-memory provider does not need installer prompts, so the default
        schema is a minimal label with no environment variables.
        """

        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }


__all__ = [
    "ApprovalLevel",
    "ApprovalStatus",
    "CRMCommitResult",
    "CRMProvider",
    "CRMWriteProposal",
    "DEFAULT_CRM_APPROVAL_LEVEL",
]
