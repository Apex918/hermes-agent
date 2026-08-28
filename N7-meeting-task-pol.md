# N7 meeting minutes -> Feishu Task PoL

This vertical slice is offline and fail-closed. It composes the N1 versioned
`DecisionRecord`, `ActionRequest`, and `WorkItem` contracts with the N4 explicit
Feishu adapter.

## Flow

1. `MeetingMinutesExtractor` accepts a mapping, bytes, or local JSON path. URL
   sources are rejected and the fixture must explicitly disable network and
   external side effects.
2. Each embedded record is validated through its N1 `from_dict` constructor.
3. `MeetingMinutesTaskPlanner` validates the task owner, ISO due date, and
   Hermes Kanban dependency IDs (`t_<id>`). Missing owner/due date and malformed
   dependencies produce `needs_input` and no Task payload.
4. Valid work items produce deterministic N4 Feishu Task previews with
   `production_write=false`, source IDs, acceptance criteria, assignee, due
   date, and `kanban.depends_on`.
5. The preview includes an N2 R2 approval plan. `apply_with_approval` rejects
   missing receipts; even with a receipt, the N4 adapter remains production
   write-disabled, so no Feishu mutation occurs.
6. A planner ledger returns `duplicate` for a repeated deterministic payload,
   preserving the first preview instead of proposing a second creation.

## Example

```python
from business.ai_business_os import MeetingMinutesTaskPlanner

planner = MeetingMinutesTaskPlanner()
previews = planner.plan_fixture("business/ai_business_os/fixtures/meeting-minutes.v1.json")
```

The checked-in PoL fixture contains five meeting examples, including valid
entries and owner/due-date `needs_input` cases.

## Verification

- `uv run --extra dev pytest tests/business/test_meeting_task_pol.py -q` -> 7 passed
- `uv run --extra dev pytest tests/business -q` -> 107 passed
- `uv run --extra dev ruff check business/ai_business_os/meeting_task_pol.py tests/business/test_meeting_task_pol.py business/ai_business_os/__init__.py` -> clean
- No Feishu write API, SDK, WebSocket, or network path is used.
