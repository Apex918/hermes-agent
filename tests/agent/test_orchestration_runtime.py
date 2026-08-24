import json

from agent.orchestration_runtime import OrchestrationRuntime
from agent.orchestrator import Orchestrator
from hermes_cli import kanban_db as kb


def test_runtime_runs_durable_preflight_then_submits_kanban(tmp_path):
    plan = Orchestrator().plan("开发一个系统并测试")
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    calls = []

    def delegate(**kwargs):
        calls.append(kwargs)
        return json.dumps({
            "results": [
                {"task_index": 0, "status": "completed", "artifacts": ["/tmp/research.md"]},
                {"task_index": 1, "status": "completed", "artifacts": ["/tmp/architecture.md"]},
            ]
        })

    with kb.connect(db_path=db_path) as conn:
        result = OrchestrationRuntime().run(
            plan,
            delegate_fn=delegate,
            parent_agent="parent",
            kanban_conn=conn,
        )
        assert result.kanban_tasks is not None
        implementation = kb.get_task(conn, result.kanban_tasks["implementation"])
        review = kb.get_task(conn, result.kanban_tasks["review"])
        assert implementation is not None
        assert review is not None

    assert len(calls) == 1
    assert result.success is True
    assert implementation.status == "ready"
    assert implementation.model_override == "gpt-5.6-luna"
    assert implementation.provider_override == "hp-fenno"
    assert review.model_override == "grok-4.6"
    assert review.provider_override == "xai-oauth"
    assert "research.md" in implementation.body


def test_runtime_preserves_failure_reason_and_evidence_on_preflight_failure(tmp_path):
    plan = Orchestrator().plan("开发一个系统并测试")
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)

    def delegate(**kwargs):
        return json.dumps({
            "results": [
                {
                    "task_index": 0,
                    "status": "needs_replan",
                    "failure_type": "quality",
                    "failure_reason": "missing evidence",
                    "evidence": {"missing": ["sources"]},
                },
                {"task_index": 1, "status": "completed", "artifacts": ["/tmp/architecture.md"]},
            ]
        })

    with kb.connect(db_path=db_path) as conn:
        result = OrchestrationRuntime().run(plan, delegate_fn=delegate, kanban_conn=conn)
        assert result.success is False
        assert result.task_results[0].status == "needs_replan"
        assert result.task_results[0].failure_reason == "missing evidence"
        assert result.task_results[0].evidence == {"missing": ["sources"]}
        assert result.kanban_tasks == {}
        assert list(kb.list_tasks(conn)) == []
