import pytest

from business.ai_business_os import (
    ApprovalError,
    ApprovalReceipt,
    ConnectorAllowlist,
    ConnectorNotAllowedError,
    FakeConnector,
    ReleaseShadowGate,
)


def _make_gate(*, initial_value: dict[str, str] | None = None) -> tuple[ReleaseShadowGate, FakeConnector]:
    connector = FakeConnector("fake-crm", initial_state=initial_value or {"customer:1": "draft"})
    gate = ReleaseShadowGate(
        connectors={"fake-crm": connector},
        allowlist=ConnectorAllowlist({"fake-crm"}),
    )
    return gate, connector


def test_reversible_write_defaults_to_shadow_only():
    gate, connector = _make_gate()

    proposal = gate.propose_reversible_write(
        connector_name="fake-crm",
        key="customer:1",
        value="ready",
    )

    assert proposal.mode == "shadow"
    assert proposal.applied is False
    assert connector.read("customer:1") == "draft"
    assert gate.audit_log[-1].status == "shadowed"
    assert gate.audit_log[-1].shadow_operation_id == proposal.operation_id


def test_commit_requires_approval_receipt_and_allowlisted_connector():
    gate, connector = _make_gate()
    proposal = gate.propose_reversible_write(
        connector_name="fake-crm",
        key="customer:1",
        value="ready",
    )

    with pytest.raises(ApprovalError):
        gate.commit_shadow_write(proposal.operation_id)

    receipt = ApprovalReceipt(
        operation_id=proposal.operation_id,
        connector_name="fake-crm",
        approved_by="ops-reviewer",
        receipt_id="receipt-001",
        approved_at="2026-08-20T09:00:00Z",
    )
    committed = gate.commit_shadow_write(proposal.operation_id, receipt=receipt)

    assert committed.status == "committed"
    assert connector.read("customer:1") == "ready"
    assert committed.receipt.receipt_id == "receipt-001"
    assert committed.receipt is not None
    assert committed.receipt.approved_by == "ops-reviewer"

    with pytest.raises(ConnectorNotAllowedError):
        gate.propose_reversible_write(
            connector_name="blocked-connector",
            key="customer:1",
            value="ignored",
        )


def test_rollback_and_replay_restore_deterministic_state():
    gate, connector = _make_gate(initial_value={"customer:1": "draft"})
    proposal = gate.propose_reversible_write(
        connector_name="fake-crm",
        key="customer:1",
        value="ready",
    )
    receipt = ApprovalReceipt(
        operation_id=proposal.operation_id,
        connector_name="fake-crm",
        approved_by="ops-reviewer",
        receipt_id="receipt-002",
        approved_at="2026-08-20T09:00:00Z",
    )
    committed = gate.commit_shadow_write(proposal.operation_id, receipt=receipt)

    rollback = gate.rollback(committed.operation_id)
    assert rollback.status == "rolled_back"
    assert connector.read("customer:1") == "draft"

    replay = gate.replay(committed.operation_id)
    assert replay.status == "replayed"
    assert connector.read("customer:1") == "ready"
    assert gate.audit_log[-1].status == "replayed"
    assert gate.audit_log[-1].source_operation_id == committed.operation_id
