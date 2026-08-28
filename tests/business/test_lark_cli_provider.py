"""N12 production lark-cli provider contract tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from business.ai_business_os.lark_cli_provider import (
    ApprovalReceiptInvalid,
    FeishuReadbackMismatch,
    LarkCLIError,
    LarkCLIProtocolError,
    LarkCLIProvider,
    PersistentWriteLedger,
    parse_ok_envelope,
)


def _proc(payload, *, returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr=stderr)


def test_strict_ok_envelope_rejects_raw_data_and_error_envelopes():
    assert parse_ok_envelope('{"ok":true,"data":{"items":[{"guid":"g1"}]}}') == {
        "items": [{"guid": "g1"}]
    }
    with pytest.raises(LarkCLIProtocolError):
        parse_ok_envelope('{"items":[{"guid":"g1"}]}')
    with pytest.raises(LarkCLIProtocolError):
        parse_ok_envelope('{"ok":false,"error":{"type":"auth"}}')


def test_reads_use_real_cli_subprocess_shape_and_never_log_stderr(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return _proc({"ok": True, "data": {"items": [{"guid": "task-1"}]}})

    provider = LarkCLIProvider(runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    assert provider.read_tasks() == [{"guid": "task-1"}]
    assert calls[0][0][:3] == ["lark-cli", "task", "+get-my-tasks"]
    assert "--dry-run" not in calls[0][0]
    assert calls[0][1]["env"] is not None


def test_real_subprocess_read_probe_parses_cli_envelope(tmp_path):
    executable = tmp_path / "lark-cli-probe"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'ok': True, 'data': {'tasks': [{'guid': 'probe-1'}]}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    provider = LarkCLIProvider(executable=str(executable), ledger_path=tmp_path / "ledger.sqlite")
    assert provider.read_tasks() == [{"guid": "probe-1"}]


def test_calendar_and_base_reads_use_explicit_resource_commands(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "calendar" in argv:
            data = {"events": [{"event_id": "e1"}]}
        else:
            data = {"records": [{"record_id": "r1"}]}
        return _proc({"ok": True, "data": data})

    provider = LarkCLIProvider(
        runner=runner, base_token="base-1", table_id="tbl-1", ledger_path=tmp_path / "ledger.sqlite"
    )
    assert provider.read_calendar() == [{"event_id": "e1"}]
    assert provider.read_base() == [{"record_id": "r1"}]
    assert calls[0][1:3] == ["calendar", "+agenda"]
    assert calls[1][1:3] == ["base", "+record-list"]
    assert "base-1" in calls[1] and "tbl-1" in calls[1]


def test_actor_can_use_the_lark_cli_bot_api(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _proc({"ok": True, "data": {"items": []}})

    provider = LarkCLIProvider(actor="bot", runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    assert provider.read_tasks() == []
    assert "--as" in calls[0]
    assert calls[0][calls[0].index("--as") + 1] == "bot"


def test_cli_process_and_timeout_errors_are_generic(tmp_path):
    def process_error(argv, **kwargs):
        return _proc({"ok": False, "error": {"message": "authorization=secret"}}, returncode=1)

    provider = LarkCLIProvider(runner=process_error, ledger_path=tmp_path / "ledger.sqlite")
    with pytest.raises(LarkCLIError, match="process error"):
        provider.read_tasks()

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    provider = LarkCLIProvider(runner=timeout, ledger_path=tmp_path / "timeout.sqlite")
    with pytest.raises(LarkCLIError, match="timed out"):
        provider.read_tasks()


def test_read_failure_is_fail_closed_and_does_not_expose_cli_stderr(tmp_path):
    def runner(argv, **kwargs):
        return _proc({"ok": False, "error": {"message": "token=super-secret"}})

    provider = LarkCLIProvider(runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    with pytest.raises(LarkCLIProtocolError) as exc:
        provider.read_tasks()
    assert "super-secret" not in str(exc.value)
    assert "token" not in str(exc.value)


def test_write_requires_receipt_target_preview_diff_rollback_and_idempotency(tmp_path):
    provider = LarkCLIProvider(runner=lambda *args, **kwargs: _proc({"ok": True, "data": {}}), ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="task",
        resource={"summary": "Canary"},
        target="task",
        preview="create task Canary",
        diff="+ Canary",
        rollback={"operation": "delete", "required": True},
        idempotency_key="n12-canary-1",
    )
    for field in ("target", "preview", "diff", "rollback", "idempotency_key"):
        bad = dict(request)
        bad[field] = "" if field != "rollback" else {}
        with pytest.raises((ValueError, ApprovalReceiptInvalid)):
            provider.write(**bad, approval_receipt="R2:operator")
    with pytest.raises(ApprovalReceiptInvalid):
        provider.write(**request, approval_receipt="invalid")


def test_task_write_readback_and_duplicate_are_apply_once(tmp_path):
    responses = iter(
        [
            _proc({"ok": True, "data": {"guid": "guid-001"}}),
            _proc({"ok": True, "data": {"guid": "guid-001", "summary": "Canary"}}),
        ]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return next(responses)

    provider = LarkCLIProvider(runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="task",
        resource={"summary": "Canary"},
        target="task:canary",
        preview="create task Canary",
        diff="+ Canary",
        rollback={"operation": "delete", "required": True},
        idempotency_key="n12-canary-1",
    )
    result = provider.write(**request, approval_receipt="R2:operator")
    assert result["status"] == "applied"
    assert result["guid"] == "guid-001"
    assert len(calls) == 2
    assert "--idempotency-key" in calls[0]
    duplicate = provider.write(**request, approval_receipt="R2:operator")
    assert duplicate["status"] == "duplicate"
    assert duplicate["idempotent"] is True
    assert len(calls) == 2


def test_idempotency_key_binds_review_metadata_as_well_as_resource(tmp_path):
    responses = iter(
        [
            _proc({"ok": True, "data": {"guid": "guid-001"}}),
            _proc({"ok": True, "data": {"guid": "guid-001"}}),
        ]
    )
    provider = LarkCLIProvider(runner=lambda *args, **kwargs: next(responses), ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="task", resource={"summary": "Canary"}, target="task:canary",
        preview="create", diff="+ Canary", rollback={"operation": "delete"},
        idempotency_key="same-key",
    )
    provider.write(**request, approval_receipt="R2:operator")
    with pytest.raises(LarkCLIError, match="different request"):
        provider.write(**{**request, "diff": "+ different"}, approval_receipt="R2:operator")


def test_calendar_write_passes_native_idempotency_key_and_calendar_id(tmp_path):
    responses = iter(
        [
            _proc({"ok": True, "data": {"event_id": "evt-001"}}),
            _proc({"ok": True, "data": {"event_id": "evt-001"}}),
        ]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return next(responses)

    provider = LarkCLIProvider(runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="calendar",
        resource={
            "calendar_id": "cal-001",
            "summary": "Canary",
            "start_time": {"timestamp": "1788004800", "timezone": "Asia/Shanghai"},
            "end_time": {"timestamp": "1788006600", "timezone": "Asia/Shanghai"},
        },
        target="calendar:cal-001",
        preview="create calendar Canary",
        diff="+ Canary",
        rollback={"operation": "delete", "required": True},
        idempotency_key="n12-calendar-idempotent-1",
    )
    provider.write(**request, approval_receipt="R3:operator")
    assert calls[0][:5] == ["lark-cli", "calendar", "events", "create", "--as"]
    assert "--idempotency-key" in calls[0]
    assert "n12-calendar-idempotent-1" in calls[0]
    assert "--calendar-id" in calls[0]
    assert "cal-001" in calls[0]
    assert calls[1][0:3] == ["lark-cli", "calendar", "+get"]


def test_concurrent_reservation_is_fail_closed(tmp_path):
    ledger = PersistentWriteLedger(tmp_path / "ledger.sqlite")
    kwargs = dict(
        idempotency_key="same-key",
        kind="task",
        target="task:1",
        audit_ref="audit:1",
        receipt="R2:operator",
        request_hash="request-hash",
    )
    assert ledger.reserve(**kwargs) is True
    assert ledger.reserve(**kwargs) is False
    assert ledger.get("same-key")["status"] == "pending"


def test_readback_mismatch_records_failed_status_without_retry(tmp_path):
    responses = iter(
        [
            _proc({"ok": True, "data": {"event_id": "evt-001"}}),
            _proc({"ok": True, "data": {"event_id": "evt-other"}}),
        ]
    )
    provider = LarkCLIProvider(runner=lambda *args, **kwargs: next(responses), ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="calendar",
        resource={"summary": "Canary", "start": "2026-09-01T00:00:00Z", "end": "2026-09-01T01:00:00Z"},
        target="calendar:primary",
        preview="create calendar Canary",
        diff="+ Canary",
        rollback={"operation": "delete", "required": True},
        idempotency_key="n12-calendar-1",
    )
    with pytest.raises(FeishuReadbackMismatch):
        provider.write(**request, approval_receipt="R3:operator")
    duplicate = provider.write(**request, approval_receipt="R3:operator")
    assert duplicate["status"] == "duplicate"
    assert duplicate["idempotent"] is True
    ledger_item = PersistentWriteLedger(tmp_path / "ledger.sqlite").get("n12-calendar-1")
    assert ledger_item["status"] == "failed"
    assert ledger_item["response_hash"]
    assert ledger_item["readback"]["event_id"] == "evt-other"


def test_base_write_is_fail_closed_when_cli_service_is_unavailable(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _proc({"ok": True, "data": {"record_id": "rec-1"}})

    provider = LarkCLIProvider(
        runner=runner, base_token="base-1", table_id="tbl-1", ledger_path=tmp_path / "ledger.sqlite"
    )
    common = {
        "target": "target",
        "preview": "create",
        "diff": "+ create",
        "rollback": {"operation": "delete"},
        "approval_receipt": "R2:operator",
    }
    with pytest.raises(LarkCLIError, match="no base service"):
        provider.write(
            kind="base", resource={"fields": {"Name": "record"}}, idempotency_key="base-key", **common
        )
    assert calls == []


def test_cli_write_error_is_ledgered_and_never_retried(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _proc({"ok": False, "error": {"message": "secret=do-not-leak"}}, returncode=1)

    provider = LarkCLIProvider(runner=runner, ledger_path=tmp_path / "ledger.sqlite")
    request = dict(
        kind="task", resource={"summary": "Canary"}, target="task:canary",
        preview="create", diff="+ create", rollback={"operation": "delete"},
        idempotency_key="n12-error-1",
    )
    with pytest.raises(LarkCLIError):
        provider.write(**request, approval_receipt="R2:operator")
    duplicate = provider.write(**request, approval_receipt="R2:operator")
    assert duplicate["status"] == "duplicate"
    assert duplicate["success"] is False
    assert len(calls) == 1


def test_ledger_is_persistent_and_does_not_store_secrets(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = PersistentWriteLedger(path)
    ledger.record(
        idempotency_key="k1",
        kind="task",
        target="task:1",
        audit_ref="audit:1",
        receipt="R2:operator",
        request_hash="req",
        response_hash="resp",
        readback={"guid": "g1"},
        status="applied",
    )
    reopened = PersistentWriteLedger(path)
    item = reopened.get("k1")
    assert item["status"] == "applied"
    raw = path.read_bytes()
    assert b"secret" not in raw
    assert b"R2:operator" in raw
