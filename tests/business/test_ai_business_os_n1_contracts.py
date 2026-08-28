"""N1 contract tests: canonical records are versioned, strict and offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from business.ai_business_os.contracts import (
    ActionRequest,
    ContractValidationError,
    DecisionRecord,
    EvidenceEnvelope,
    WorkItem,
)
from business.ai_business_os.validator import validate_named_contract


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = ROOT / "business" / "ai_business_os" / "contracts"


def evidence_payload() -> dict:
    return {
        "schema_version": "hermes.ai_business_os.evidence_envelope.v1",
        "manifest_version": 1,
        "evidence_id": "evidence-001",
        "source": "internal-ledger",
        "source_id": "ledger-2026-q3",
        "as_of": "2026-08-28T00:00:00Z",
        "freshness": {"reviewed_at": "2026-08-28", "max_age_days": 30},
        "license_or_entitlement": "internal-enterprise",
        "pii_class": "none",
        "confidence": 0.98,
        "allowed_use": ["internal_analysis", "decision_support"],
        "hash": "sha256:" + "a" * 64,
        "evidence": [{"kind": "report", "summary": "Quarterly ledger export"}],
    }


def decision_payload() -> dict:
    return {
        "schema_version": "hermes.ai_business_os.decision_record.v1",
        "manifest_version": 1,
        "decision_id": "decision-001",
        "owner": "cfo",
        "approver": "finance-owner",
        "review_date": "2026-08-28",
        "decision": "approved",
        "rationale": "Evidence supports the bounded internal change.",
        "evidence_refs": ["evidence-001"],
    }


def action_payload() -> dict:
    return {
        "schema_version": "hermes.ai_business_os.action_request.v1",
        "manifest_version": 1,
        "request_id": "action-001",
        "owner": "operations",
        "effect_level": "R2",
        "preview": {"target": "kanban", "operation": "update-status"},
        "rollback": {"operation": "restore-status", "required": True},
        "approvers": ["operations-owner"],
        "idempotency_key": "action-001-v1",
        "audit_ref": "audit:action-001",
        "action": "update-work-item",
        "evidence_refs": ["evidence-001"],
    }


def work_item_payload() -> dict:
    return {
        "schema_version": "hermes.ai_business_os.work_item.v1",
        "manifest_version": 1,
        "work_item_id": "work-001",
        "title": "Validate quarterly plan",
        "owner": "chief_of_staff",
        "effect_level": "R1",
        "acceptance_criteria": ["Decision record is versioned", "Evidence is traceable"],
        "idempotency_key": "work-001-v1",
        "audit_ref": "audit:work-001",
        "status": "ready",
        "priority": "P1",
        "evidence_refs": ["evidence-001"],
    }


@pytest.mark.parametrize(
    ("filename", "version"),
    [
        ("evidence-envelope.v1.schema.json", "hermes.ai_business_os.evidence_envelope.v1"),
        ("decision-record.v1.schema.json", "hermes.ai_business_os.decision_record.v1"),
        ("action-request.v1.schema.json", "hermes.ai_business_os.action_request.v1"),
        ("work-item.v1.schema.json", "hermes.ai_business_os.work_item.v1"),
    ],
)
def test_all_n1_contracts_are_versioned(filename: str, version: str) -> None:
    schema = json.loads((CONTRACT_DIR / filename).read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == version
    assert schema["properties"]["manifest_version"]["const"] == 1
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "payload_factory"),
    [
        ("evidence-envelope", evidence_payload),
        ("decision-record", decision_payload),
        ("action-request", action_payload),
        ("work-item", work_item_payload),
    ],
)
def test_canonical_contracts_validate(name: str, payload_factory) -> None:
    assert validate_named_contract(name, payload_factory()) == []


@pytest.mark.parametrize(
    ("name", "payload_factory", "field"),
    [
        ("evidence-envelope", evidence_payload, "source_id"),
        ("evidence-envelope", evidence_payload, "license_or_entitlement"),
        ("evidence-envelope", evidence_payload, "allowed_use"),
        ("decision-record", decision_payload, "owner"),
        ("decision-record", decision_payload, "review_date"),
        ("action-request", action_payload, "effect_level"),
        ("action-request", action_payload, "preview"),
        ("action-request", action_payload, "rollback"),
        ("action-request", action_payload, "approvers"),
        ("action-request", action_payload, "idempotency_key"),
        ("action-request", action_payload, "audit_ref"),
        ("work-item", work_item_payload, "acceptance_criteria"),
        ("work-item", work_item_payload, "idempotency_key"),
    ],
)
def test_missing_canonical_field_fails_closed(name: str, payload_factory, field: str) -> None:
    payload = payload_factory()
    payload.pop(field)
    assert any(field in error for error in validate_named_contract(name, payload))


def test_unknown_fields_fail_closed() -> None:
    payload = work_item_payload()
    payload["unexpected"] = True
    assert validate_named_contract("work-item", payload)


def test_contract_objects_round_trip_and_read_parent_artifact(tmp_path: Path) -> None:
    evidence = EvidenceEnvelope.from_dict(evidence_payload())
    decision = DecisionRecord.from_dict(decision_payload())
    action = ActionRequest.from_dict(action_payload())
    artifact = {"artifact": {"work_item": work_item_payload()}}
    artifact_path = tmp_path / "parent-artifact.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    item = WorkItem.from_parent_artifact(artifact_path)
    assert evidence.to_dict()["source_id"] == "ledger-2026-q3"
    assert decision.to_dict()["owner"] == "cfo"
    assert action.to_dict()["idempotency_key"] == "action-001-v1"
    assert item.to_dict() == work_item_payload()


def test_invalid_contract_object_raises_validation_error() -> None:
    with pytest.raises(ContractValidationError):
        WorkItem.from_dict({"work_item_id": "missing-version"})


def test_parent_artifact_reader_rejects_network_sources() -> None:
    with pytest.raises(ContractValidationError, match="network artifact"):
        WorkItem.from_parent_artifact("https://example.invalid/parent.json")
