import json
from typing import cast

from agent.orchestrator import Orchestrator
from agent.orchestration_runtime import OrchestrationRunResult
from agent.workflow_adapter import WorkflowAdapter, WorkflowReceipt
from hermes_cli import kanban_db as kb


def _delegate_response():
    return json.dumps({
        "results": [
            {"task_index": 0, "status": "completed", "artifacts": ["/tmp/research.md"]},
            {"task_index": 1, "status": "completed", "artifacts": ["/tmp/architecture.md"]},
        ]
    })


def test_workflow_adapter_resume_once_creates_kanban_graph_and_receipt(tmp_path):
    plan = Orchestrator().plan("开发一个系统并测试", total_time_budget_seconds=120)
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    adapter = WorkflowAdapter()
    delegate_calls = []

    def delegate(**kwargs):
        delegate_calls.append(kwargs)
        return _delegate_response()

    receipt = adapter.start(plan, delegate_fn=delegate, parent_agent="parent")
    assert receipt.phase == "awaiting_kanban"
    assert receipt.resume_count == 0
    assert receipt.graph["plan_id"] == plan.plan_id
    assert receipt.graph["links"]
    assert receipt.completed is False
    assert len(delegate_calls) == 1

    with kb.connect(db_path=db_path) as conn:
        assert list(kb.list_tasks(conn)) == []
        result = adapter.resume(receipt, kanban_conn=conn)
        implementation = kb.get_task(conn, result.kanban_tasks["implementation"])
        review = kb.get_task(conn, result.kanban_tasks["review"])
        assert implementation is not None
        assert review is not None
        child_ids = kb.child_ids(conn, implementation.id)
        child_task = kb.get_task(conn, child_ids[0])
        assert child_task is not None
        assert child_task.title.startswith("运行测试")
        assert "research.md" in (implementation.body or "")
        assert result.success is True
        assert result.resumed is True
        assert result.resume_count == 1
        assert len(list(kb.list_tasks(conn))) == 3

        again = adapter.resume(receipt, kanban_conn=conn)
        assert again.kanban_tasks == result.kanban_tasks
        assert again.resume_count == 1
        assert len(list(kb.list_tasks(conn))) == 3


def test_workflow_adapter_receipt_round_trips_across_restart(tmp_path):
    plan = Orchestrator().plan("开发一个系统并测试", total_time_budget_seconds=120)
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    adapter = WorkflowAdapter()

    receipt = adapter.start(plan, delegate_fn=lambda **kwargs: _delegate_response(), parent_agent="parent")
    restored = WorkflowReceipt.from_dict(receipt.to_dict())
    fresh_adapter = WorkflowAdapter()

    with kb.connect(db_path=db_path) as conn:
        result = fresh_adapter.resume(restored, kanban_conn=conn)
        assert result.success is True
        assert result.resume_count == 1
        assert restored.completed is True
        assert restored.resume_count == 1
        assert len(list(kb.list_tasks(conn))) == 3
        again = fresh_adapter.resume(restored, kanban_conn=conn)
        assert again.resume_count == 1


def test_workflow_adapter_run_completes_without_interrupt(tmp_path):
    plan = Orchestrator().plan("开发一个系统并测试", total_time_budget_seconds=120)
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    adapter = WorkflowAdapter()

    with kb.connect(db_path=db_path) as conn:
        result = cast(
            OrchestrationRunResult,
            adapter.run(
                plan,
                delegate_fn=lambda **kwargs: _delegate_response(),
                kanban_conn=conn,
                parent_agent="parent",
            ),
        )
        assert result.success is True
        assert result.completed is True
        assert result.resume_count == 1
        assert result.resumed is True
        assert len(list(kb.list_tasks(conn))) == 3
