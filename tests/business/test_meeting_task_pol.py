"""N7 PoL: meeting minutes become guarded Feishu Task previews."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from business.ai_business_os import (
    MeetingFixtureValidationError,
    MeetingMinutesExtractor,
    MeetingMinutesTaskPlanner,
    TaskCreationPreview,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "business" / "ai_business_os" / "fixtures" / "meeting-minutes.v1.json"


def test_fixture_extracts_five_meetings_and_all_n1_record_types() -> None:
    meetings = MeetingMinutesExtractor().extract(FIXTURE)

    assert len(meetings) == 5
    assert all(meeting.decisions for meeting in meetings)
    assert all(meeting.action_requests for meeting in meetings)
    assert all(meeting.work_items for meeting in meetings)
    assert meetings[0].decisions[0].decision_id == "decision-001"
    assert meetings[0].action_requests[0].request_id == "action-001"
    assert meetings[0].work_items[0].work_item.work_item_id == "work-001"


def test_planner_builds_feishu_task_previews_with_kanban_dependencies() -> None:
    planner = MeetingMinutesTaskPlanner()
    previews = planner.plan_fixture(FIXTURE)

    assert len(previews) == 5
    valid = [item for item in previews if item.status == "preview"]
    assert len(valid) == 3
    first = valid[0]
    assert first.task_preview is not None
    resource = first.task_preview["payload"]["resource"]
    assert resource["summary"] == "Publish the operations evidence checklist"
    assert resource["assignee_open_id"] == "operations"
    assert resource["due_date"] == "2026-09-05"
    assert first.task_preview["production_write"] is False
    assert first.approval_plan is not None
    assert first.approval_plan["approval_level"] == "R2"
    assert first.approval_plan["receipt_required"] is True
    assert first.kanban == {
        "depends_on": ["t_parent001"],
        "dependency_count": 1,
        "source_work_item_id": "work-001",
    }


def test_missing_owner_and_due_date_fail_closed_to_needs_input() -> None:
    previews = MeetingMinutesTaskPlanner().plan_fixture(FIXTURE)

    owner_missing = next(item for item in previews if item.work_item_id == "work-003")
    due_missing = next(item for item in previews if item.work_item_id == "work-004")
    assert owner_missing.status == "needs_input"
    assert owner_missing.missing_fields == ("owner",)
    assert owner_missing.task_preview is None
    assert due_missing.status == "needs_input"
    assert due_missing.missing_fields == ("due_date",)
    assert due_missing.task_preview is None


def test_planning_same_fixture_twice_is_idempotent_and_does_not_create_duplicates() -> None:
    planner = MeetingMinutesTaskPlanner()

    first = planner.plan_fixture(FIXTURE)
    second = planner.plan_fixture(FIXTURE)
    first_valid = [item for item in first if item.status == "preview"]
    second_duplicates = [item for item in second if item.status == "duplicate"]

    assert len(first_valid) == 3
    assert len(second_duplicates) == 3
    assert all(item.duplicate_of for item in second_duplicates)
    assert all(item.task_preview == first_valid[index].task_preview for index, item in enumerate(second_duplicates))


def test_n2_receipt_gate_blocks_without_receipt_and_keeps_production_write_disabled() -> None:
    planner = MeetingMinutesTaskPlanner()
    planned = next(item for item in planner.plan_fixture(FIXTURE) if item.status == "preview")

    missing = planner.apply_with_approval(planned)
    assert missing["status"] == "pending_approval"
    assert missing["success"] is False
    assert missing["production_write"] is False

    approved_but_disabled = planner.apply_with_approval(planned, approval_receipt="receipt:n2:operator")
    assert approved_but_disabled["status"] == "blocked"
    assert approved_but_disabled["success"] is False
    assert approved_but_disabled["production_write"] is False


def test_invalid_network_source_and_dependency_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(MeetingFixtureValidationError, match="network"):
        MeetingMinutesExtractor().extract("https://example.invalid/minutes.json")

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["meetings"][0]["work_items"][0]["kanban_dependencies"] = ["not-a-kanban-id"]
    path = tmp_path / "invalid-dependency.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    result = MeetingMinutesTaskPlanner().plan_fixture(path)
    assert result[0].status == "needs_input"
    assert "invalid Hermes Kanban dependency id" in result[0].errors[0]


def test_preview_objects_are_structured_contracts() -> None:
    planner = MeetingMinutesTaskPlanner()
    result = planner.plan_fixture(FIXTURE)[0]

    assert isinstance(result, TaskCreationPreview)
    serialized = result.to_dict()
    assert serialized["status"] == "preview"
    assert serialized["task_preview"]["schema_version"] == "hermes.ai_business_os.feishu_explicit.v1"
