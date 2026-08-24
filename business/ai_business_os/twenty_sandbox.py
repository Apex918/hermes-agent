"""Offline Twenty sandbox proof built on the CRM provider contract.

This module stays fully local: it reads a fixture, records the legal/license
gate decision in the proposal path, and only permits commits when the sandbox
fixture says the evidence was verified and the caller supplies an approval
receipt. No external services are contacted.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .approval_state_machine import ApprovalLevel, ApprovalPlan, ApprovalStateMachine
from .crm_provider import (
    CRMCommitResult,
    CRMProvider,
    CRMWriteProposal,
    DEFAULT_CRM_APPROVAL_LEVEL,
)

DEFAULT_TWENTY_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "twenty_sandbox.local.json"


@dataclass(frozen=True)
class TwentySandboxProof:
    """End-to-end proof bundle for an offline Twenty sandbox run."""

    gate_decision: str
    license_evidence_verified: bool
    license_evidence_source: str
    read_rows: tuple[dict[str, Any], ...]
    proposal: CRMWriteProposal
    commit_result: Optional[CRMCommitResult] = None
    audit_log: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class TwentySandboxProvider(CRMProvider):
    """Fake Twenty provider that never leaves the local filesystem.

    The provider is intentionally conservative: when the fixture does not prove
    license evidence, the proposal path is marked as proposal-only/dry-run and
    commit attempts fail closed.
    """

    def __init__(
        self,
        *,
        fixture_path: str | Path | None = None,
        production_credential: str = "",
        name: str = "twenty-sandbox",
        records: List[Dict[str, Any]] | None = None,
    ) -> None:
        self._name = name
        self._fixture_path = Path(fixture_path) if fixture_path is not None else DEFAULT_TWENTY_FIXTURE_PATH
        self._fixture = load_twenty_sandbox_fixture(self._fixture_path)
        seed_records = records if records is not None else self._fixture.get("records", [])
        self._records: list[dict[str, Any]] = [deepcopy(record) for record in seed_records]
        self._state_machine = ApprovalStateMachine()
        self._applied: dict[str, CRMCommitResult] = {}
        self._audit_log: list[dict[str, Any]] = []
        self._production_credential = (production_credential or "").strip()
        evidence = self._fixture.get("license_evidence") or {}
        self._license_evidence_verified = bool(evidence.get("verified", False))
        self._license_evidence_source = str(evidence.get("source") or self._fixture_path)
        self._license_evidence_reason = str(
            evidence.get("reason")
            or evidence.get("notes")
            or ("license evidence verified" if self._license_evidence_verified else "license evidence inconclusive")
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def license_evidence_verified(self) -> bool:
        return self._license_evidence_verified

    @property
    def license_evidence_source(self) -> str:
        return self._license_evidence_source

    @property
    def gate_decision(self) -> str:
        return "verified" if self._license_evidence_verified else "proposal-only"

    @property
    def audit_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(entry) for entry in self._audit_log)

    def is_available(self) -> bool:
        return True

    def read(self, query: str, *, limit: int = 20, **kwargs: Any) -> List[Dict[str, Any]]:
        q = (query or "").strip().lower()
        rows: list[dict[str, Any]] = []
        for record in self._records:
            haystack = json.dumps(record, sort_keys=True, ensure_ascii=False).lower()
            if not q or q in haystack:
                rows.append(deepcopy(record))
        result = rows[: max(0, int(limit))]
        self._audit_log.append(
            {
                "event": "read",
                "query": query,
                "limit": int(limit),
                "results": len(result),
                "gate_decision": self.gate_decision,
                "license_evidence_verified": self._license_evidence_verified,
                "license_evidence_source": self._license_evidence_source,
            }
        )
        return result

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
        sandbox_only = bool(kwargs.get("sandbox_only", True))
        production_credential_present = bool(self._production_credential or kwargs.get("production_credential"))
        effective_dry_run = bool(dry_run or not sandbox_only or production_credential_present or not self._license_evidence_verified)
        gate_reason = self._gate_reason(production_credential_present=production_credential_present)
        plan = self._state_machine.plan(
            subsystem="crm",
            action=operation,
            payload={
                "operation": operation,
                "payload": deepcopy(payload),
                "gate_decision": self.gate_decision,
                "license_evidence_verified": self._license_evidence_verified,
                "license_evidence_source": self._license_evidence_source,
                "production_credential_present": bool(self._production_credential),
            },
            summary=summary,
            origin=origin,
            dry_run=effective_dry_run,
            approval_level=approval_level,
        )
        proposal = CRMWriteProposal(
            operation=operation,
            payload=deepcopy(payload),
            summary=summary,
            origin=origin,
            approval_level=plan.approval_level,
            receipt_required=plan.receipt_required,
            preview=plan.preview,
            diff=plan.diff,
            rollback=plan.rollback,
            audit_ref=plan.audit_ref,
            idempotency_key=plan.idempotency_key,
            status=plan.status,
            dry_run=plan.dry_run,
            reason=gate_reason,
            gate_decision=self.gate_decision,
        )
        self._audit_log.append(
            {
                "event": "propose",
                "operation": operation,
                "gate_decision": proposal.gate_decision,
                "license_evidence_verified": self._license_evidence_verified,
                "license_evidence_source": self._license_evidence_source,
                "approval_level": proposal.approval_level,
                "receipt_required": proposal.receipt_required,
                "status": proposal.status,
                "dry_run": proposal.dry_run,
                "reason": proposal.reason,
                "audit_ref": proposal.audit_ref,
                "idempotency_key": proposal.idempotency_key,
            }
        )
        return proposal

    def commit(
        self,
        proposal: CRMWriteProposal,
        *,
        approval_receipt: str,
        idempotency_key: str,
        **kwargs: Any,
    ) -> CRMCommitResult:
        production_credential = (kwargs.get("production_credential") or self._production_credential or "").strip()
        if production_credential:
            return self._reject_and_record(
                proposal=proposal,
                idempotency_key=idempotency_key,
                approval_receipt=approval_receipt,
                error="production credential refused in sandbox",
                status="rejected",
            )

        if proposal.dry_run or not self._license_evidence_verified:
            return self._reject_and_record(
                proposal=proposal,
                idempotency_key=idempotency_key,
                approval_receipt=approval_receipt,
                error=f"proposal-only: {self._license_evidence_reason}",
                status=proposal.status if proposal.dry_run else "rejected",
            )

        if not approval_receipt:
            return self._reject_and_record(
                proposal=proposal,
                idempotency_key=idempotency_key,
                approval_receipt="",
                error="approval receipt required",
                status="rejected",
            )
        if idempotency_key != proposal.idempotency_key:
            return self._reject_and_record(
                proposal=proposal,
                idempotency_key=idempotency_key,
                approval_receipt=approval_receipt,
                error="idempotency key mismatch",
                status="rejected",
            )

        cached = self._applied.get(idempotency_key)
        if cached is not None:
            replay = CRMCommitResult(
                success=True,
                status=cached.status,
                operation=cached.operation,
                approval_level=cached.approval_level,
                approval_receipt=approval_receipt,
                idempotency_key=idempotency_key,
                preview=cached.preview,
                diff=cached.diff,
                rollback=cached.rollback,
                audit_ref=cached.audit_ref,
                result=deepcopy(cached.result),
                error="",
                idempotent=True,
                gate_decision=cached.gate_decision,
            )
            self._audit_log.append(
                {
                    "event": "commit",
                    "operation": proposal.operation,
                    "success": True,
                    "idempotent": True,
                    "gate_decision": replay.gate_decision,
                    "approval_receipt": approval_receipt,
                    "idempotency_key": idempotency_key,
                    "audit_ref": proposal.audit_ref,
                }
            )
            return replay

        def effect() -> dict[str, Any]:
            payload = deepcopy(proposal.payload)
            record_id = str(payload.get("id") or payload.get("email") or f"record-{len(self._records) + 1}")
            record = {"id": record_id, **payload}
            self._records = [row for row in self._records if row.get("id") != record_id]
            self._records.append(record)
            self._audit_log.append(
                {
                    "event": "apply",
                    "operation": proposal.operation,
                    "record_id": record_id,
                    "record": deepcopy(record),
                }
            )
            return {"record": deepcopy(record), "records": deepcopy(self._records)}

        outcome = self._state_machine.apply_once(_proposal_to_plan(proposal), effect, receipt=approval_receipt)
        result = CRMCommitResult(
            success=bool(outcome.get("success", False)),
            status=outcome.get("status", "approved"),
            operation=proposal.operation,
            approval_level=proposal.approval_level,
            approval_receipt=approval_receipt,
            idempotency_key=idempotency_key,
            preview=proposal.preview,
            diff=proposal.diff,
            rollback=proposal.rollback,
            audit_ref=proposal.audit_ref,
            result=outcome.get("result"),
            error=outcome.get("error", ""),
            idempotent=bool(outcome.get("idempotent", False)),
            gate_decision=proposal.gate_decision,
        )
        if result.success:
            self._applied[idempotency_key] = result
        self._audit_log.append(
            {
                "event": "commit",
                "operation": proposal.operation,
                "success": result.success,
                "status": result.status,
                "gate_decision": result.gate_decision,
                "approval_receipt": approval_receipt,
                "idempotency_key": idempotency_key,
                "audit_ref": proposal.audit_ref,
                "idempotent": result.idempotent,
                "error": result.error,
            }
        )
        return result

    def _reject_and_record(
        self,
        *,
        proposal: CRMWriteProposal,
        idempotency_key: str,
        approval_receipt: str,
        error: str,
        status: str,
    ) -> CRMCommitResult:
        result = CRMCommitResult(
            success=False,
            status=status,  # type: ignore[arg-type]
            operation=proposal.operation,
            approval_level=proposal.approval_level,
            approval_receipt=approval_receipt,
            idempotency_key=idempotency_key,
            preview=proposal.preview,
            diff=proposal.diff,
            rollback=proposal.rollback,
            audit_ref=proposal.audit_ref,
            error=error,
            gate_decision=proposal.gate_decision,
        )
        self._audit_log.append(
            {
                "event": "commit",
                "operation": proposal.operation,
                "success": False,
                "status": status,
                "gate_decision": proposal.gate_decision,
                "approval_receipt": approval_receipt,
                "idempotency_key": idempotency_key,
                "audit_ref": proposal.audit_ref,
                "error": error,
            }
        )
        return result

    def _gate_reason(self, *, production_credential_present: bool = False) -> str:
        if self._production_credential or production_credential_present:
            return "proposal-only: production credential refused in sandbox"
        if self._license_evidence_verified:
            return f"license evidence verified locally from {self._license_evidence_source}; sandbox-only proof"
        return f"proposal-only: {self._license_evidence_reason} ({self._license_evidence_source})"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "offline",
            "tag": "twenty-sandbox",
            "env_vars": [],
        }


FakeTwentyProvider = TwentySandboxProvider
FakeTwentyCRMProvider = TwentySandboxProvider


def build_twenty_sandbox_proof(
    *,
    fixture_path: str | Path | None = None,
    query: str = "Twenty",
    limit: int = 20,
    operation: str = "upsert_contact",
    payload: Dict[str, Any] | None = None,
    summary: str = "offline Twenty sandbox proof",
    origin: str = "foreground",
    approval_receipt: str = "twenty-sandbox-receipt",
    idempotency_key: str | None = None,
    production_credential: str = "",
) -> TwentySandboxProof:
    provider = TwentySandboxProvider(
        fixture_path=fixture_path,
        production_credential=production_credential,
    )
    read_rows = provider.read(query, limit=limit)
    write_payload = payload or _default_twenty_payload(read_rows)
    proposal = provider.propose_write(
        operation,
        write_payload,
        summary=summary,
        origin=origin,
    )
    commit_result = provider.commit(
        proposal,
        approval_receipt=approval_receipt,
        idempotency_key=idempotency_key or proposal.idempotency_key,
        production_credential=production_credential,
    )
    return TwentySandboxProof(
        gate_decision=provider.gate_decision,
        license_evidence_verified=provider.license_evidence_verified,
        license_evidence_source=provider.license_evidence_source,
        read_rows=tuple(deepcopy(row) for row in read_rows),
        proposal=proposal,
        commit_result=commit_result,
        audit_log=provider.audit_log,
    )


def load_twenty_sandbox_fixture(path: str | Path | None = None) -> Dict[str, Any]:
    fixture_path = Path(path) if path is not None else DEFAULT_TWENTY_FIXTURE_PATH
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {
        "license_evidence": {
            "verified": False,
            "source": str(fixture_path),
            "reason": "fixture missing or unreadable",
        },
        "records": [],
    }


def _proposal_to_plan(proposal: CRMWriteProposal) -> ApprovalPlan:
    return ApprovalPlan(
        subsystem="crm",
        action=proposal.operation,
        approval_level=proposal.approval_level,
        receipt_required=proposal.receipt_required,
        preview=proposal.preview,
        diff=proposal.diff,
        rollback=proposal.rollback,
        audit_ref=proposal.audit_ref,
        idempotency_key=proposal.idempotency_key,
        status=proposal.status,
        dry_run=proposal.dry_run,
        receipt="",
        reason=proposal.reason,
    )


def _default_twenty_payload(read_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(read_rows)
    source = rows[0] if rows else {"id": "twenty-sandbox-1", "name": "Twenty Sandbox", "company": "Local Fixture"}
    return {
        "id": source.get("id") or "twenty-sandbox-1",
        "name": source.get("name") or "Twenty Sandbox",
        "company": source.get("company") or "Local Fixture",
        "email": source.get("email") or "twenty-sandbox@example.com",
    }


__all__ = [
    "DEFAULT_TWENTY_FIXTURE_PATH",
    "FakeTwentyCRMProvider",
    "FakeTwentyProvider",
    "TwentySandboxProof",
    "TwentySandboxProvider",
    "build_twenty_sandbox_proof",
    "load_twenty_sandbox_fixture",
]
