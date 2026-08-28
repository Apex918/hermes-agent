"""Contract tests for the explicit Feishu adapter and lark-cli command."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from business.ai_business_os import (
    FakeFeishuProvider,
    FeishuExplicitAdapter,
    ProductionWriteDisabled,
    WebhookIngressDisabled,
)


REPO = Path(__file__).resolve().parents[2]


def _adapter() -> tuple[FeishuExplicitAdapter, FakeFeishuProvider]:
    provider = FakeFeishuProvider(
        tasks=[{"task_id": "task-1", "summary": "Review release", "access_token": "secret"}],
        calendar=[{"event_id": "event-1", "summary": "Review evidence"}],
        base=[{"record_id": "record-1", "fields": {"Status": "Ready"}}],
    )
    return FeishuExplicitAdapter(provider), provider


def test_explicit_reads_are_provider_calls_and_return_copies():
    adapter, provider = _adapter()

    assert adapter.read_tasks()[0]["task_id"] == "task-1"
    assert adapter.read_calendar()[0]["event_id"] == "event-1"
    assert adapter.read_base()[0]["record_id"] == "record-1"
    assert provider.calls == ["read_tasks", "read_calendar", "read_base"]

    result = adapter.read_tasks()
    result[0]["summary"] = "mutated"
    assert adapter.read_tasks()[0]["summary"] == "Review release"


def test_previews_are_deterministic_redacted_and_never_production_writes():
    adapter, _ = _adapter()

    first = adapter.preview_task(adapter.read_tasks()[0])
    second = adapter.preview_task(adapter.read_tasks()[0])
    assert first == second
    assert first["mode"] == "dry_run"
    assert first["production_write"] is False
    assert first["payload"]["resource"]["access_token"] == "<redacted>"
    assert first["idempotency_key"].startswith("feishu-explicit:local:task:create:")
    assert first["audit_ref"].startswith("audit:")

    assert adapter.create_task({"task_id": "task-2"})["mode"] == "dry_run"
    with pytest.raises(ProductionWriteDisabled):
        adapter.create_task({"task_id": "task-2"}, dry_run=False)


def test_preview_all_covers_task_calendar_and_base():
    adapter, _ = _adapter()
    result = adapter.preview_all()
    assert set(result) == {"tasks", "calendar", "base"}
    assert result["tasks"][0]["kind"] == "task"
    assert result["calendar"][0]["kind"] == "calendar"
    assert result["base"][0]["kind"] == "base"


def test_ima_archive_is_a_preview_and_webhook_is_optional():
    adapter, _ = _adapter()
    archive = adapter.archive_ima_report("daily report", report_id="r-1")
    assert archive["kind"] == "ima_report"
    assert archive["operation"] == "archive"
    assert archive["production_write"] is False

    with pytest.raises(WebhookIngressDisabled):
        adapter.ingest_webhook_event({"kind": "task", "payload": {"task_id": "t"}})

    enabled = FeishuExplicitAdapter(FakeFeishuProvider(), webhook_enabled=True)
    event = enabled.ingest_webhook_event({"kind": "task", "payload": {"task_id": "t"}})
    assert event["kind"] == "task"
    assert event["operation"] == "ingest"
    assert event["production_write"] is False


def test_preview_all_classifies_event_fixture_by_kind_and_count():
    fixture = REPO / "business" / "ai_business_os" / "fixtures" / "feishu-events.v1.json"
    provider = FakeFeishuProvider.from_fixture(json.loads(fixture.read_text(encoding="utf-8")))
    result = FeishuExplicitAdapter(provider).preview_all()

    assert {kind: len(items) for kind, items in result.items()} == {
        "tasks": 1,
        "calendar": 1,
        "base": 1,
    }
    assert result["tasks"][0]["payload"]["resource"]["task_id"] == "task_fixture_001"
    assert result["calendar"][0]["payload"]["resource"]["event_id"] == "cal_event_fixture_001"
    assert result["base"][0]["payload"]["resource"]["record_id"] == "rec_fixture_001"


def test_real_lark_cli_commands_emit_contract_json():
    fixture = REPO / "business" / "ai_business_os" / "fixtures" / "feishu-events.v1.json"
    command = [sys.executable, "-m", "business.ai_business_os.lark_cli", "--fixture", str(fixture)]

    read = subprocess.run(command + ["read", "tasks", "--json"], cwd=REPO, text=True, capture_output=True, check=False)
    assert read.returncode == 0, read.stderr
    tasks = json.loads(read.stdout)
    assert tasks[0]["task_id"] == "task_fixture_001"

    preview = subprocess.run(command + ["preview", "all", "--json"], cwd=REPO, text=True, capture_output=True, check=False)
    assert preview.returncode == 0, preview.stderr
    previews = json.loads(preview.stdout)
    assert set(previews) == {"tasks", "calendar", "base"}
    assert {kind: len(items) for kind, items in previews.items()} == {
        "tasks": 1,
        "calendar": 1,
        "base": 1,
    }
    assert previews["calendar"][0]["payload"]["resource"]["event_id"] == "cal_event_fixture_001"
    assert all(item["production_write"] is False for items in previews.values() for item in items)

    ima = subprocess.run(
        [sys.executable, "-m", "business.ai_business_os.lark_cli", "ima", "archive", "--report", "report text", "--json"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert ima.returncode == 0, ima.stderr
    assert json.loads(ima.stdout)["kind"] == "ima_report"
