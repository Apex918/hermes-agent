from __future__ import annotations

import json
from pathlib import Path

import pytest

from business.ai_business_os.twenty_sandbox import (
    FakeTwentyCRMProvider,
    TwentySandboxProvider,
    build_twenty_sandbox_proof,
    load_twenty_sandbox_fixture,
)


def _write_fixture(path: Path, *, verified: bool) -> Path:
    payload = {
        "license_evidence": {
            "verified": verified,
            "source": str(path),
            "reason": "verified locally" if verified else "proposal-only",
        },
        "records": [
            {
                "id": "twenty-verified-1",
                "name": "Ada Lovelace",
                "company": "Analytical Engines",
                "email": "ada@example.com",
            },
            {
                "id": "twenty-verified-2",
                "name": "Grace Hopper",
                "company": "Compilers Co",
                "email": "grace@example.com",
            },
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_default_fixture_is_proposal_only():
    fixture = load_twenty_sandbox_fixture()
    assert fixture["license_evidence"]["verified"] is False
    provider = TwentySandboxProvider()
    rows = provider.read("Twenty", limit=2)
    assert len(rows) >= 1
    proposal = provider.propose_write(
        "upsert_contact",
        {"id": "twenty-003", "name": "Katherine Johnson", "email": "katherine@example.com"},
        summary="twenty sandbox proof",
    )
    assert proposal.gate_decision == "proposal-only"
    assert proposal.dry_run is True
    assert "proposal-only" in proposal.reason.lower()
    assert provider.audit_log[0]["event"] == "read"
    assert provider.audit_log[1]["event"] == "propose"


def test_verified_fixture_requires_receipt_and_is_replayable(tmp_path):
    fixture_path = _write_fixture(tmp_path / "twenty-verified.json", verified=True)
    provider = TwentySandboxProvider(fixture_path=fixture_path)
    proposal = provider.propose_write(
        "upsert_contact",
        {"id": "twenty-003", "name": "Katherine Johnson", "email": "katherine@example.com"},
        summary="verified sandbox proof",
    )

    assert proposal.gate_decision == "verified"
    assert proposal.dry_run is False
    assert proposal.receipt_required is True
    assert "verified locally" in proposal.reason.lower()

    missing_receipt = provider.commit(
        proposal,
        approval_receipt="",
        idempotency_key=proposal.idempotency_key,
    )
    assert missing_receipt.success is False
    assert "receipt" in missing_receipt.error.lower()

    wrong_credential = provider.commit(
        proposal,
        approval_receipt="approval-123",
        idempotency_key=proposal.idempotency_key,
        production_credential="prod-secret",
    )
    assert wrong_credential.success is False
    assert "production" in wrong_credential.error.lower()

    committed = provider.commit(
        proposal,
        approval_receipt="approval-123",
        idempotency_key=proposal.idempotency_key,
    )
    assert committed.success is True
    assert committed.idempotent is False
    assert committed.gate_decision == "verified"
    assert committed.result["record"]["name"] == "Katherine Johnson"

    replay = provider.commit(
        proposal,
        approval_receipt="approval-123",
        idempotency_key=proposal.idempotency_key,
    )
    assert replay.success is True
    assert replay.idempotent is True
    assert replay.result == committed.result
    assert provider.read("Katherine") == [
        {"id": "twenty-003", "name": "Katherine Johnson", "email": "katherine@example.com"}
    ]
    assert provider.audit_log[-1]["event"] == "read"


def test_proposal_only_fixture_fails_closed_even_with_receipt(tmp_path):
    fixture_path = _write_fixture(tmp_path / "twenty-proposal-only.json", verified=False)
    provider = TwentySandboxProvider(fixture_path=fixture_path)
    proposal = provider.propose_write(
        "upsert_contact",
        {"id": "twenty-004", "name": "Alan Turing", "email": "alan@example.com"},
        summary="proposal-only sandbox proof",
    )

    result = provider.commit(
        proposal,
        approval_receipt="approval-456",
        idempotency_key=proposal.idempotency_key,
    )
    assert result.success is False
    assert "proposal-only" in result.error.lower()
    assert provider.audit_log[-1]["event"] == "commit"


def test_build_twenty_sandbox_proof_captures_the_full_path(tmp_path):
    fixture_path = _write_fixture(tmp_path / "twenty-proof.json", verified=True)
    proof = build_twenty_sandbox_proof(
        fixture_path=fixture_path,
        query="Twenty",
        operation="upsert_contact",
        payload={"id": "twenty-005", "name": "Katherine Johnson", "email": "katherine@example.com"},
        approval_receipt="approval-789",
    )

    assert proof.gate_decision == "verified"
    assert proof.license_evidence_verified is True
    assert proof.proposal.gate_decision == "verified"
    assert proof.commit_result is not None and proof.commit_result.success is True
    assert proof.commit_result.gate_decision == "verified"
    assert proof.read_rows
    assert [entry["event"] for entry in proof.audit_log][:2] == ["read", "propose"]


@pytest.mark.parametrize("provider_cls", [TwentySandboxProvider, FakeTwentyCRMProvider])
def test_fake_aliases_share_the_same_behavior(provider_cls, tmp_path):
    fixture_path = _write_fixture(tmp_path / "twenty-alias.json", verified=True)
    provider = provider_cls(fixture_path=fixture_path)
    proposal = provider.propose_write(
        "upsert_contact",
        {"id": "twenty-006", "name": "Barbara Liskov", "email": "barbara@example.com"},
        summary="alias behavior",
    )
    assert proposal.gate_decision == "verified"
    commit = provider.commit(
        proposal,
        approval_receipt="approval-999",
        idempotency_key=proposal.idempotency_key,
    )
    assert commit.success is True
