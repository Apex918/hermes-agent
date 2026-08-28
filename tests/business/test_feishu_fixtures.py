"""Hermetic Feishu event corpus tests.

These tests exercise the shipped import paths and parsers against a checked-in
fixture.  They intentionally do not inspect implementation source text or use
live Feishu credentials/services.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from business.ai_business_os import (
    EVENT_KINDS,
    FeishuFixtureScope,
    FeishuFixtureValidationError,
    OfflineFeishuEventStore,
    load_feishu_event_fixture,
    redact_sensitive_fields,
)
from plugins.platforms.feishu.adapter import normalize_feishu_message
from plugins.platforms.feishu.feishu_comment import (
    handle_drive_comment_event,
    parse_drive_comment_event,
)


@pytest.fixture(scope="module")
def feishu_events():
    return load_feishu_event_fixture()


def _event(feishu_events, kind):
    return next(event for event in feishu_events["events"] if event["kind"] == kind)


def test_fixture_is_complete_versioned_and_network_closed(feishu_events):
    assert feishu_events["schema_version"] == "hermes.ai_business_os.feishu_events.v1"
    assert feishu_events["manifest_version"] == 1
    assert feishu_events["network_policy"] == {
        "network_allowed": False,
        "external_side_effects": False,
        "source": "checked-in-local-fixture",
    }
    assert {event["kind"] for event in feishu_events["events"]} == set(EVENT_KINDS)
    assert len({event["event_id"] for event in feishu_events["events"]}) == len(feishu_events["events"])
    assert len({event["idempotency_key"] for event in feishu_events["events"]}) == len(feishu_events["events"])


def test_fixture_events_use_real_feishu_import_paths(feishu_events):
    im = _event(feishu_events, "im")
    normalized_im = normalize_feishu_message(**{
        "message_type": im["payload"]["message_type"],
        "raw_content": im["payload"]["raw_content"],
    })
    assert normalized_im.raw_type == "text"
    assert "offline IM fixture" in normalized_im.text_content

    comment = _event(feishu_events, "document_comment")
    parsed_comment = parse_drive_comment_event(SimpleNamespace(event=comment["payload"]["event"]))
    assert parsed_comment == {
        "event_id": comment["event_id"],
        "comment_id": "comment_fixture_001",
        "reply_id": "reply_fixture_001",
        "is_mentioned": True,
        "timestamp": "1787875200",
        "file_token": "docx_fixture_token",
        "file_type": "docx",
        "notice_type": "add_reply",
        "from_open_id": "ou_fixture_user",
        "to_open_id": "ou_fixture_bot",
    }

    card = _event(feishu_events, "card")
    normalized_card = normalize_feishu_message(
        message_type=card["payload"]["message_type"],
        raw_content=card["payload"]["raw_content"],
    )
    assert normalized_card.relation_kind == "interactive"
    assert "Offline approval card" in normalized_card.text_content
    assert "Approve" in normalized_card.text_content


def test_scope_rejects_tenant_profile_workspace_and_path_crossing(feishu_events):
    scope = FeishuFixtureScope.from_fixture(feishu_events)
    original = _event(feishu_events, "task")

    for field, value in (
        ("tenant_id", "tenant-fixture-b"),
        ("profile", "another-profile"),
        ("workspace_id", "workspace-fixture-b"),
        ("workspace_path", "tenant-fixture-b/feishu-offline"),
        ("artifact_path", "tenant-fixture-a/other-profile/event.json"),
        ("artifact_path", "tenant-fixture-a/feishu-offline/../escape.json"),
        ("artifact_path", "https://evil.invalid/event.json"),
    ):
        candidate = deepcopy(original)
        candidate[field] = value
        with pytest.raises(PermissionError):
            scope.authorize(candidate)

    scope.authorize(original)


def test_idempotency_is_deterministic_and_no_external_side_effects(feishu_events):
    store = OfflineFeishuEventStore(FeishuFixtureScope.from_fixture(feishu_events))
    results = []
    for event in feishu_events["events"]:
        results.append(store.ingest(event))
        duplicate = store.ingest(event)
        assert duplicate == {
            "status": "duplicate",
            "applied": False,
            "idempotency_key": event["idempotency_key"],
        }

    assert [result["status"] for result in results] == ["accepted"] * len(EVENT_KINDS)
    assert all(result["applied"] is False for result in results)
    assert len(store.accepted_events) == len(EVENT_KINDS)
    accepted_comment = next(item for item in store.accepted_events if item["kind"] == "document_comment")
    assert accepted_comment["payload"]["event"]["notice_meta"]["file_token"] == "<redacted>"
    assert store.external_side_effects == []


def test_sensitive_fields_are_redacted_without_mutating_fixture(feishu_events):
    original = deepcopy(feishu_events)
    redacted = redact_sensitive_fields(feishu_events)

    assert feishu_events == original
    assert set(redacted["sensitive_fields"].values()) == {"<redacted>"}
    comment = _event(redacted, "document_comment")
    assert comment["payload"]["event"]["notice_meta"]["file_token"] == "<redacted>"
    base = _event(redacted, "base")
    assert base["payload"]["app_token"] == "<redacted>"
    assert base["payload"]["record_id"] == "rec_fixture_001"


def test_fixture_loader_rejects_network_sources():
    with pytest.raises(FeishuFixtureValidationError, match="network fixture"):
        load_feishu_event_fixture("https://example.invalid/feishu-events.json")


def test_fixture_loader_rejects_network_payloads(tmp_path, feishu_events):
    candidate = deepcopy(feishu_events)
    candidate["events"][0]["payload"]["external_url"] = "https://example.invalid/side-effect"
    path = tmp_path / "network-payload.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(FeishuFixtureValidationError, match="network source"):
        load_feishu_event_fixture(path)


@pytest.mark.asyncio
async def test_denied_document_comment_has_no_external_side_effects(feishu_events):
    """The real comment handler must stop before any API operation."""

    comment = _event(feishu_events, "document_comment")
    payload = SimpleNamespace(event=deepcopy(comment["payload"]["event"]))
    payload.event["notice_meta"]["to_user_id"]["open_id"] = "ou_other_bot"
    client = Mock()

    await handle_drive_comment_event(client, payload, self_open_id="ou_fixture_bot")

    client.request.assert_not_called()
    client.post.assert_not_called()
    client.send.assert_not_called()
