from __future__ import annotations

import json
from pathlib import Path

import pytest

from business.ai_business_os.approval_state_machine import ApprovalStateMachine
from tools import write_approval as wa


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_dry_run_exposes_preview_diff_rollback_and_audit_ref():
    machine = ApprovalStateMachine()
    plan = machine.plan(
        subsystem="skills",
        action="edit",
        payload={"action": "edit", "name": "demo", "content": "---\ndescription: demo\n---\nbody\n"},
        summary="edit skill demo",
        origin="foreground",
        dry_run=True,
    )

    assert plan.status == "dry_run"
    assert plan.approval_level == "R3"
    assert plan.receipt_required is True
    assert plan.preview
    assert plan.diff
    assert plan.rollback
    assert plan.audit_ref


def test_r2_and_r3_reject_without_receipt():
    machine = ApprovalStateMachine()
    plan = machine.plan(
        subsystem="memory",
        action="replace",
        payload={"action": "replace", "target": "user", "content": "new", "old_text": "old"},
        summary="replace a memory",
        origin="foreground",
    )

    assert plan.approval_level == "R2"
    assert plan.receipt_required is True

    result = machine.apply_once(plan, lambda: {"success": True})

    assert result["success"] is False
    assert "receipt" in result["error"].lower()


def test_apply_once_is_idempotent_for_the_same_key():
    machine = ApprovalStateMachine()
    plan = machine.plan(
        subsystem="memory",
        action="add",
        payload={"action": "add", "target": "user", "content": "remember me"},
        summary="add a memory",
        origin="foreground",
        receipt="receipt-123",
    )

    calls: list[str] = []

    def effect():
        calls.append("applied")
        return {"success": True, "side_effect": len(calls)}

    first = machine.apply_once(plan, effect, receipt="receipt-123")
    second = machine.apply_once(plan, effect, receipt="receipt-123")

    assert first["success"] is True
    assert first["side_effect"] == 1
    assert second["success"] is True
    assert second["idempotent"] is True
    assert calls == ["applied"]


def test_apply_pending_is_idempotent_with_receipt(hermes_home):
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "receipt ok"},
        summary="force R2",
        origin="foreground",
        approval_level="R2",
    )

    calls: list[str] = []

    def effect():
        calls.append("applied")
        return {"success": True, "count": len(calls)}

    first = wa.apply_pending(record, effect, receipt="receipt-123")
    second = wa.apply_pending(record, effect, receipt="receipt-123")

    assert first["success"] is True
    assert first["count"] == 1
    assert second["success"] is True
    assert second["idempotent"] is True
    assert second["count"] == 1
    assert calls == ["applied"]


def test_apply_pending_rejects_r2_records_without_receipt(hermes_home):
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "receipt check"},
        summary="force R2",
        origin="foreground",
        approval_level="R2",
    )

    outcome = wa.apply_pending(record, lambda: {"success": True})

    assert outcome["success"] is False
    assert "receipt" in outcome["error"].lower()


def test_stage_write_records_preview_diff_rollback_and_audit_ref(hermes_home):
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "remember the launch"},
        summary="add memory",
        origin="foreground",
    )

    assert record["preview"]
    assert record["diff"]
    assert record["rollback"]
    assert record["audit_ref"]
    assert record["idempotency_key"]
    assert record["receipt_required"] is False

    pending = Path(hermes_home) / "pending" / "memory" / f"{record['id']}.json"
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["audit_ref"] == record["audit_ref"]
    assert data["preview"] == record["preview"]


def test_stage_write_dry_run_does_not_persist_pending_record(hermes_home):
    record = wa.stage_write(
        wa.SKILLS,
        {"action": "edit", "name": "demo", "content": "---\ndescription: demo\n---\nbody\n"},
        summary="dry run skill edit",
        origin="foreground",
        dry_run=True,
    )

    assert record["dry_run"] is True
    assert record["staged"] is False
    assert record["preview"]
    assert record["audit_ref"]
    pending_dir = Path(hermes_home) / "pending" / "skills"
    assert not pending_dir.exists() or not list(pending_dir.glob("*.json"))


def test_state_machine_records_approval_apply_lifecycle():
    machine = ApprovalStateMachine()
    plan = machine.plan(
        subsystem="skills",
        action="edit",
        payload={"name": "demo", "content": "new"},
        summary="edit demo",
        origin="foreground",
    )

    approved = machine.approve(plan, receipt="receipt-real-1")
    assert approved.status == "approved"
    assert approved.state_history == ("pending_approval", "approved")

    result = machine.apply_once(approved, lambda: {"success": True, "value": 7}, receipt="receipt-real-1")

    assert result["status"] == "applied"
    assert result["state_history"] == ["pending_approval", "approved", "applying", "applied"]


def test_state_machine_records_failed_apply_and_rejects_bypass_markers():
    machine = ApprovalStateMachine()
    plan = machine.plan(
        subsystem="crm",
        action="upsert",
        payload={"id": "c1"},
        summary="upsert contact",
        origin="foreground",
        approval_level="R3",
    )

    rejected = machine.apply_once(plan, lambda: {"success": True}, receipt="--yolo")
    assert rejected["status"] == "rejected"
    assert rejected["state_history"] == ["pending_approval", "rejected"]

    approved = machine.approve(plan, receipt="receipt-real-2")
    failed = machine.apply_once(approved, lambda: {"success": False, "error": "provider down"}, receipt="receipt-real-2")
    assert failed["status"] == "failed"
    assert failed["state_history"] == ["pending_approval", "approved", "applying", "failed"]


def test_approval_record_is_readable_after_apply(hermes_home):
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "replace", "target": "user", "old_text": "old", "content": "new"},
        summary="replace memory",
        origin="foreground",
        approval_level="R2",
    )
    outcome = wa.apply_pending(record, lambda: {"success": True, "changed": True}, receipt="receipt-real-3")
    assert outcome["status"] == "applied"
    audit = wa.read_approval(record["id"])
    assert audit["audit_ref"] == record["audit_ref"]
    assert audit["status"] == "applied"
    assert audit["approval_receipt"] == "receipt-real-3"


def test_apply_pending_uses_durable_ledger_after_state_machine_restart(hermes_home):
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "once only"},
        summary="once only",
        origin="foreground",
        approval_level="R2",
    )
    calls: list[str] = []

    first = wa.apply_pending(record, lambda: calls.append("write") or {"success": True}, receipt="receipt-real-4")
    wa.DEFAULT_STATE_MACHINE.clear()
    second = wa.apply_pending(record, lambda: calls.append("duplicate") or {"success": True}, receipt="receipt-real-4")

    assert first["status"] == "applied"
    assert second["idempotent"] is True
    assert calls == ["write"]


def test_reject_pending_is_auditable(hermes_home):
    record = wa.stage_write(
        wa.SKILLS,
        {"action": "delete", "name": "demo"},
        summary="delete demo",
        origin="foreground",
    )

    assert wa.reject_pending(wa.SKILLS, record["id"], reason="not authorized") is True
    audit = wa.read_approval(record["id"])
    assert audit is not None
    assert audit["status"] == "rejected"
    assert audit["reason"] == "not authorized"
