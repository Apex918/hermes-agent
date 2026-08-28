"""Offline meeting-minutes to Feishu Task preview proof of life.

The module joins the versioned N1 records with the N4 explicit Feishu adapter.
It only reads local fixtures, creates deterministic Task previews, and exposes
an N2 receipt gate for the (intentionally disabled) production write path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .approval_state_machine import ApprovalPlan, ApprovalStateMachine
from .contracts import ActionRequest, ContractValidationError, DecisionRecord, WorkItem
from .feishu_adapter import (
    FeishuAdapterError,
    FeishuAdapterScope,
    FeishuExplicitAdapter,
    FakeFeishuProvider,
    ProductionWriteDisabled,
)

MEETING_FIXTURE_SCHEMA = "hermes.ai_business_os.meeting_minutes.v1"
KANBAN_TASK_ID = re.compile(r"^t_[A-Za-z0-9][A-Za-z0-9_-]*$")


class MeetingFixtureValidationError(ValueError):
    """Raised when a local meeting fixture is malformed or unsafe."""


@dataclass(frozen=True)
class MeetingWorkItem:
    """A validated WorkItem plus meeting-specific routing metadata.

    ``owner_override`` is allowed to be blank in a minutes fixture so the
    planner can demonstrate its ``needs_input`` path while the embedded N1
    WorkItem remains a valid, strictly versioned record.
    """

    work_item: WorkItem
    due_date: str | None
    kanban_dependencies: tuple[str, ...]
    owner_override: str | None = None


@dataclass(frozen=True)
class MeetingMinutes:
    """Validated records extracted from one meeting-minutes fixture entry."""

    meeting_id: str
    title: str
    meeting_date: str
    decisions: tuple[DecisionRecord, ...]
    action_requests: tuple[ActionRequest, ...]
    work_items: tuple[MeetingWorkItem, ...]


@dataclass(frozen=True)
class TaskCreationPreview:
    """Review envelope for one possible Feishu Task creation."""

    meeting_id: str
    work_item_id: str
    status: str
    missing_fields: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    task_preview: Mapping[str, Any] | None = None
    approval_plan: Mapping[str, Any] | None = None
    kanban: Mapping[str, Any] | None = None
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "work_item_id": self.work_item_id,
            "status": self.status,
            "missing_fields": list(self.missing_fields),
            "errors": list(self.errors),
            "task_preview": dict(self.task_preview) if self.task_preview is not None else None,
            "approval_plan": dict(self.approval_plan) if self.approval_plan is not None else None,
            "kanban": dict(self.kanban) if self.kanban is not None else None,
            "duplicate_of": self.duplicate_of,
        }


class MeetingMinutesExtractor:
    """Read only local fixtures and construct strict N1 contract objects."""

    @staticmethod
    def load(source: Mapping[str, Any] | str | Path | bytes) -> dict[str, Any]:
        if isinstance(source, Mapping):
            value = dict(source)
        elif isinstance(source, bytes):
            try:
                value = json.loads(source.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MeetingFixtureValidationError("fixture bytes must contain JSON") from exc
        else:
            if isinstance(source, str) and "://" in source:
                raise MeetingFixtureValidationError("network meeting fixtures are not permitted")
            try:
                value = json.loads(Path(source).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MeetingFixtureValidationError(f"cannot read local meeting fixture: {source}") from exc
        if not isinstance(value, dict):
            raise MeetingFixtureValidationError("meeting fixture root must be an object")
        if value.get("schema_version") != MEETING_FIXTURE_SCHEMA:
            raise MeetingFixtureValidationError("unsupported meeting fixture schema version")
        if value.get("manifest_version") != 1:
            raise MeetingFixtureValidationError("unsupported meeting fixture manifest version")
        policy = value.get("network_policy")
        if not isinstance(policy, Mapping) or policy.get("network_allowed") is not False or policy.get("external_side_effects") is not False:
            raise MeetingFixtureValidationError("meeting fixture must disable network and external side effects")
        meetings = value.get("meetings")
        if not isinstance(meetings, list) or not meetings:
            raise MeetingFixtureValidationError("meeting fixture must contain meetings")
        return value

    def extract(self, source: Mapping[str, Any] | str | Path | bytes) -> tuple[MeetingMinutes, ...]:
        fixture = self.load(source)
        result: list[MeetingMinutes] = []
        seen_meeting_ids: set[str] = set()
        for raw in fixture["meetings"]:
            if not isinstance(raw, Mapping):
                raise MeetingFixtureValidationError("meeting entry must be an object")
            meeting_id = raw.get("meeting_id")
            title = raw.get("title")
            meeting_date = raw.get("meeting_date")
            if not all(isinstance(value, str) and value.strip() for value in (meeting_id, title, meeting_date)):
                raise MeetingFixtureValidationError("meeting_id, title, and meeting_date are required")
            if meeting_id in seen_meeting_ids:
                raise MeetingFixtureValidationError(f"duplicate meeting_id: {meeting_id}")
            _parse_due_date(meeting_date, field="meeting_date")
            decisions = tuple(self._decision(item) for item in _list_field(raw, "decisions"))
            actions = tuple(self._action(item) for item in _list_field(raw, "action_requests"))
            work_items = tuple(self._work_item(item) for item in _list_field(raw, "work_items"))
            result.append(MeetingMinutes(meeting_id, title, meeting_date, decisions, actions, work_items))
            seen_meeting_ids.add(meeting_id)
        return tuple(result)

    @staticmethod
    def _decision(item: Any) -> DecisionRecord:
        if not isinstance(item, Mapping):
            raise MeetingFixtureValidationError("decision entry must be an object")
        payload = item.get("decision_record", item)
        if not isinstance(payload, Mapping):
            raise MeetingFixtureValidationError("decision_record must be an object")
        try:
            return DecisionRecord.from_dict(payload)
        except ContractValidationError as exc:
            raise MeetingFixtureValidationError(str(exc)) from exc

    @staticmethod
    def _action(item: Any) -> ActionRequest:
        if not isinstance(item, Mapping):
            raise MeetingFixtureValidationError("action request entry must be an object")
        payload = item.get("action_request", item)
        if not isinstance(payload, Mapping):
            raise MeetingFixtureValidationError("action_request must be an object")
        try:
            return ActionRequest.from_dict(payload)
        except ContractValidationError as exc:
            raise MeetingFixtureValidationError(str(exc)) from exc

    @staticmethod
    def _work_item(item: Any) -> MeetingWorkItem:
        if not isinstance(item, Mapping):
            raise MeetingFixtureValidationError("work item entry must be an object")
        payload = item.get("work_item", item)
        if not isinstance(payload, Mapping):
            raise MeetingFixtureValidationError("work_item must be an object")
        try:
            work_item = WorkItem.from_dict(payload)
        except ContractValidationError as exc:
            raise MeetingFixtureValidationError(str(exc)) from exc
        due_date = item.get("due_date")
        if due_date is not None and not isinstance(due_date, str):
            raise MeetingFixtureValidationError("due_date must be an ISO date or datetime")
        dependencies = item.get("kanban_dependencies", ())
        if not isinstance(dependencies, (list, tuple)) or any(not isinstance(value, str) for value in dependencies):
            raise MeetingFixtureValidationError("kanban_dependencies must be a list of strings")
        return MeetingWorkItem(
            work_item,
            due_date,
            tuple(dependencies),
            item.get("owner") if "owner" in item else None,
        )


def _list_field(value: Mapping[str, Any], field: str) -> list[Any]:
    result = value.get(field, [])
    if not isinstance(result, list):
        raise MeetingFixtureValidationError(f"{field} must be a list")
    return result


def _parse_due_date(value: str | None, *, field: str = "due_date") -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        parsed_date = date.fromisoformat(text)
        return parsed_date.isoformat()
    except ValueError:
        pass
    try:
        parsed_datetime = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MeetingFixtureValidationError(f"{field} must be ISO-8601 date or datetime") from exc
    return parsed_datetime.isoformat()


def _validate_dependencies(values: Sequence[str]) -> tuple[str, ...]:
    errors = [value for value in values if not KANBAN_TASK_ID.fullmatch(value)]
    if errors:
        raise MeetingFixtureValidationError("invalid Hermes Kanban dependency id: " + ", ".join(errors))
    return tuple(dict.fromkeys(values))


def _approval_dict(plan: ApprovalPlan) -> dict[str, Any]:
    return {
        "status": plan.status,
        "approval_level": plan.approval_level,
        "receipt_required": plan.receipt_required,
        "audit_ref": plan.audit_ref,
        "idempotency_key": plan.idempotency_key,
        "preview": plan.preview,
        "diff": plan.diff,
        "rollback": plan.rollback,
        "production_write": False,
    }


class MeetingMinutesTaskPlanner:
    """Generate idempotent Task previews and keep the write path fail-closed."""

    def __init__(
        self,
        adapter: FeishuExplicitAdapter | None = None,
        *,
        approval_machine: ApprovalStateMachine | None = None,
    ) -> None:
        self.adapter = adapter or FeishuExplicitAdapter(
            FakeFeishuProvider(), scope=FeishuAdapterScope(profile="meeting-minutes")
        )
        self.approval_machine = approval_machine or ApprovalStateMachine()
        self._planned_by_key: dict[str, TaskCreationPreview] = {}

    def plan_fixture(self, source: Mapping[str, Any] | str | Path | bytes) -> list[TaskCreationPreview]:
        meetings = MeetingMinutesExtractor().extract(source)
        return [preview for meeting in meetings for preview in self.plan_meeting(meeting)]

    def plan_meeting(self, meeting: MeetingMinutes) -> list[TaskCreationPreview]:
        results: list[TaskCreationPreview] = []
        for item in meeting.work_items:
            results.append(self._plan_item(meeting, item))
        return results

    def _plan_item(self, meeting: MeetingMinutes, item: MeetingWorkItem) -> TaskCreationPreview:
        work = item.work_item
        missing: list[str] = []
        errors: list[str] = []
        owner = item.owner_override if item.owner_override is not None else work.owner
        if not isinstance(owner, str) or not owner.strip():
            missing.append("owner")
        try:
            due_date = _parse_due_date(item.due_date)
        except MeetingFixtureValidationError as exc:
            errors.append(str(exc))
            due_date = None
        if due_date is None:
            missing.append("due_date")
        try:
            dependencies = _validate_dependencies(item.kanban_dependencies)
        except MeetingFixtureValidationError as exc:
            errors.append(str(exc))
            dependencies = ()
        if missing or errors:
            return TaskCreationPreview(
                meeting.meeting_id,
                work.work_item_id,
                "needs_input",
                tuple(missing),
                tuple(errors),
                kanban={"depends_on": list(dependencies), "dependency_count": len(dependencies)},
            )

        task = {
            "task_id": f"meeting-{meeting.meeting_id}-{work.work_item_id}",
            "summary": work.title,
            "description": "\n".join(work.acceptance_criteria),
            "assignee_open_id": owner,
            "due_date": due_date,
            "source_meeting_id": meeting.meeting_id,
            "source_work_item_id": work.work_item_id,
            "evidence_refs": list(work.evidence_refs),
        }
        task_preview = self.adapter.preview_task(task)
        approval = self.approval_machine.plan(
            subsystem="feishu",
            action="create_task",
            payload=task,
            summary=f"Create Feishu Task for meeting work item {work.work_item_id}",
            origin=f"meeting:{meeting.meeting_id}",
            approval_level="R2",
            dry_run=True,
        )
        key = str(task_preview["idempotency_key"])
        kanban = {
            "depends_on": list(dependencies),
            "dependency_count": len(dependencies),
            "source_work_item_id": work.work_item_id,
        }
        if key in self._planned_by_key:
            first = self._planned_by_key[key]
            return TaskCreationPreview(
                meeting.meeting_id,
                work.work_item_id,
                "duplicate",
                task_preview=first.task_preview,
                approval_plan=first.approval_plan,
                kanban=kanban,
                duplicate_of=key,
            )
        result = TaskCreationPreview(
            meeting.meeting_id,
            work.work_item_id,
            "preview",
            task_preview=task_preview,
            approval_plan=_approval_dict(approval),
            kanban=kanban,
        )
        self._planned_by_key[key] = result
        return result

    def apply_with_approval(
        self,
        planned: TaskCreationPreview,
        *,
        approval_receipt: str | None = None,
    ) -> dict[str, Any]:
        """Exercise N2 approval without enabling a production Feishu write."""

        if planned.status != "preview" or planned.task_preview is None or planned.approval_plan is None:
            return {"success": False, "status": planned.status, "production_write": False}
        if not approval_receipt:
            return {
                "success": False,
                "status": "pending_approval",
                "error": "R2 approval receipt required",
                "audit_ref": planned.approval_plan["audit_ref"],
                "production_write": False,
            }
        task = planned.task_preview["payload"]["resource"]
        plan = self.approval_machine.plan(
            subsystem="feishu",
            action="create_task",
            payload=dict(task),
            summary=f"Create Feishu Task for meeting work item {planned.work_item_id}",
            origin=f"meeting:{planned.meeting_id}",
            approval_level="R2",
            dry_run=False,
        )
        try:
            outcome = self.approval_machine.apply_once(
                plan,
                lambda: self.adapter.create_task(task, dry_run=False),
                receipt=approval_receipt,
            )
            if outcome.get("status") == "failed" and "production Feishu writes are disabled" in outcome.get("error", ""):
                outcome["status"] = "blocked"
                outcome["production_write"] = False
            return outcome
        except ProductionWriteDisabled as exc:
            return {
                "success": False,
                "status": "blocked",
                "error": str(exc),
                "audit_ref": plan.audit_ref,
                "idempotency_key": plan.idempotency_key,
                "approval_level": plan.approval_level,
                "production_write": False,
            }
        except FeishuAdapterError as exc:
            return {
                "success": False,
                "status": "failed",
                "error": str(exc),
                "audit_ref": plan.audit_ref,
                "idempotency_key": plan.idempotency_key,
                "production_write": False,
            }


__all__ = [
    "KANBAN_TASK_ID",
    "MEETING_FIXTURE_SCHEMA",
    "MeetingFixtureValidationError",
    "MeetingMinutes",
    "MeetingMinutesExtractor",
    "MeetingMinutesTaskPlanner",
    "MeetingWorkItem",
    "TaskCreationPreview",
]
