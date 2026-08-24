from __future__ import annotations

import json
from copy import deepcopy

import pytest

from business.ai_business_os.approval_state_machine import ApprovalPlan, ApprovalStateMachine
from business.ai_business_os.crm_provider import (
    CRMCommitResult,
    CRMProvider,
    CRMWriteProposal,
)


class FakeCRMProvider(CRMProvider):
    """In-memory CRM provider used to exercise the contract without side effects."""

    def __init__(self, name: str = "fake-crm", available: bool = True):
        self._name = name
        self._available = available
        self._state_machine = ApprovalStateMachine()
        self._records = [
            {"id": "acct-1", "name": "Ada Lovelace", "email": "ada@example.com", "company": "Analytical Engines"},
            {"id": "acct-2", "name": "Grace Hopper", "email": "grace@example.com", "company": "Compilers Co"},
        ]
        self._applied: dict[str, CRMCommitResult] = {}
        self._snapshots: list[tuple[str, dict[str, object]]] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def read(self, query: str, *, limit: int = 20, **kwargs):
        q = (query or "").strip().lower()
        rows = []
        for record in self._records:
            haystack = json.dumps(record, sort_keys=True).lower()
            if not q or q in haystack:
                rows.append(deepcopy(record))
        return rows[: max(0, int(limit))]

    def propose_write(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        summary: str = "",
        origin: str = "foreground",
        dry_run: bool = False,
        approval_level=None,
    ) -> CRMWriteProposal:
        plan = self._state_machine.plan(
            subsystem="crm",
            action=operation,
            payload={"operation": operation, "payload": payload},
            summary=summary,
            origin=origin,
            dry_run=dry_run,
            approval_level=approval_level or "R2",
        )
        return CRMWriteProposal(
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
            reason=plan.reason,
        )

    def commit(
        self,
        proposal: CRMWriteProposal,
        *,
        approval_receipt: str,
        idempotency_key: str,
        **kwargs,
    ) -> CRMCommitResult:
        if proposal.receipt_required and not approval_receipt:
            return CRMCommitResult(
                success=False,
                status="rejected",
                operation=proposal.operation,
                approval_level=proposal.approval_level,
                approval_receipt="",
                idempotency_key=idempotency_key,
                preview=proposal.preview,
                diff=proposal.diff,
                rollback=proposal.rollback,
                audit_ref=proposal.audit_ref,
                error="approval receipt required",
            )
        if idempotency_key != proposal.idempotency_key:
            return CRMCommitResult(
                success=False,
                status="rejected",
                operation=proposal.operation,
                approval_level=proposal.approval_level,
                approval_receipt=approval_receipt,
                idempotency_key=idempotency_key,
                preview=proposal.preview,
                diff=proposal.diff,
                rollback=proposal.rollback,
                audit_ref=proposal.audit_ref,
                error="idempotency key mismatch",
            )

        cached = self._applied.get(idempotency_key)
        if cached is not None:
            return CRMCommitResult(
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
                idempotent=True,
            )

        def effect():
            payload = deepcopy(proposal.payload)
            record_id = str(payload.get("id") or payload.get("email") or f"record-{len(self._records) + 1}")
            record = {"id": record_id, **payload}
            self._records = [row for row in self._records if row.get("id") != record_id]
            self._records.append(record)
            self._snapshots.append((proposal.operation, deepcopy(record)))
            return {"record": deepcopy(record), "records": deepcopy(self._records)}

        outcome = self._state_machine.apply_once(proposal_to_plan(proposal), effect, receipt=approval_receipt)
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
        )
        if result.success:
            self._applied[idempotency_key] = result
        return result


def proposal_to_plan(proposal: CRMWriteProposal) -> ApprovalPlan:
    """Adapt the public CRM proposal to the shared approval state machine."""

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


class TestCRMProviderABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            CRMProvider()

    def test_concrete_provider_reports_available(self):
        provider = FakeCRMProvider()
        assert provider.name == "fake-crm"
        assert provider.is_available() is True

    def test_read_is_side_effect_free(self):
        provider = FakeCRMProvider()
        before = deepcopy(provider._records)
        assert provider.read("Ada") == [before[0]]
        assert provider._records == before


class TestCRMWriteContract:
    def test_propose_write_returns_approval_metadata(self):
        provider = FakeCRMProvider()
        proposal = provider.propose_write(
            "upsert_contact",
            {"id": "acct-3", "name": "Katherine Johnson", "email": "katherine@example.com"},
            summary="upsert a CRM contact",
        )

        assert proposal.operation == "upsert_contact"
        assert proposal.approval_level == "R2"
        assert proposal.receipt_required is True
        assert proposal.preview
        assert proposal.diff
        assert proposal.rollback
        assert proposal.audit_ref
        assert proposal.idempotency_key

    def test_commit_requires_receipt_and_is_idempotent(self):
        provider = FakeCRMProvider()
        proposal = provider.propose_write(
            "upsert_contact",
            {"id": "acct-3", "name": "Katherine Johnson", "email": "katherine@example.com"},
            summary="upsert a CRM contact",
        )

        missing_receipt = provider.commit(
            proposal,
            approval_receipt="",
            idempotency_key=proposal.idempotency_key,
        )
        assert missing_receipt.success is False
        assert "receipt" in missing_receipt.error.lower()

        wrong_key = provider.commit(
            proposal,
            approval_receipt="approval-123",
            idempotency_key="wrong-key",
        )
        assert wrong_key.success is False
        assert "idempotency" in wrong_key.error.lower()

        committed = provider.commit(
            proposal,
            approval_receipt="approval-123",
            idempotency_key=proposal.idempotency_key,
        )
        assert committed.success is True
        assert committed.status == "approved"
        assert committed.idempotent is False
        assert committed.result["record"]["name"] == "Katherine Johnson"
        assert provider.read("Katherine") == [
            {"id": "acct-3", "name": "Katherine Johnson", "email": "katherine@example.com"}
        ]

        replay = provider.commit(
            proposal,
            approval_receipt="approval-123",
            idempotency_key=proposal.idempotency_key,
        )
        assert replay.success is True
        assert replay.idempotent is True
        assert replay.result == committed.result
        assert provider.read("Katherine") == [
            {"id": "acct-3", "name": "Katherine Johnson", "email": "katherine@example.com"}
        ]
