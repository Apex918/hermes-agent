import json

import pytest

from agent.orchestration_backends import DelegateBackend
from agent.orchestrator import (
    Orchestrator,
    PlanValidationError,
    RoutePolicy,
    TaskResult,
)


def test_simple_request_routes_to_direct_plan():
    plan = Orchestrator().plan("解释这个函数的作用")

    assert plan.execution_mode == "direct"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].role == "coordinator"
    assert plan.tasks[0].backend == "direct"


def test_cross_domain_build_request_creates_parallel_then_sequential_dag():
    plan = Orchestrator().plan("研究数据源并开发一个金融晨报系统，最后测试和审查")

    assert plan.execution_mode == "durable"
    assert {task.id for task in plan.tasks} == {
        "research",
        "architecture",
        "implementation",
        "testing",
        "review",
    }
    assert plan.task("implementation").depends_on == ("research", "architecture")
    assert plan.task("testing").depends_on == ("implementation",)
    assert plan.task("review").depends_on == ("testing",)
    assert plan.task("implementation").backend == "kanban"
    assert plan.task("research").backend == "delegate_task"


def test_parallel_research_uses_delegate_task_without_persistence():
    plan = Orchestrator().plan("比较三个方案并调研最新资料")

    assert plan.execution_mode == "fanout"
    assert len(plan.tasks) == 3
    assert all(task.backend == "delegate_task" for task in plan.tasks)
    assert all(task.depends_on == () for task in plan.tasks)


def test_route_policy_rejects_unregistered_role():
    policy = RoutePolicy()

    with pytest.raises(PlanValidationError, match="unregistered role"):
        policy.route(role="unknown", requested_backend="kanban")


def test_plan_rejects_unknown_dependency_and_cycles():
    with pytest.raises(PlanValidationError, match="unknown dependency"):
        Orchestrator().validate_plan(
            {
                "plan_id": "p1",
                "objective": "x",
                "execution_mode": "durable",
                "tasks": [
                    {"id": "a", "title": "A", "role": "coder", "depends_on": ["missing"]}
                ],
            }
        )

    with pytest.raises(PlanValidationError, match="cycle"):
        Orchestrator().validate_plan(
            {
                "plan_id": "p1",
                "objective": "x",
                "execution_mode": "durable",
                "tasks": [
                    {"id": "a", "title": "A", "role": "coder", "depends_on": ["b"]},
                    {"id": "b", "title": "B", "role": "tester", "depends_on": ["a"]},
                ],
            }
        )


def test_replan_is_bounded_and_changes_only_recoverable_failures():
    orchestrator = Orchestrator(max_replans=1)
    result = TaskResult(
        task_id="implementation",
        status="needs_replan",
        failure_type="quality",
        failure_reason="missing acceptance evidence",
        evidence={"review": "needs more proof"},
    )

    replanned = orchestrator.replan(
        Orchestrator().plan("开发一个系统并测试"),
        [result],
    )

    assert replanned.replan_count == 1
    assert replanned.task("implementation").status == "ready"
    assert replanned.task("review").status == "pending"

    with pytest.raises(PlanValidationError, match="human intervention"):
        orchestrator.replan(
            replanned,
            [TaskResult(task_id="implementation", status="blocked", failure_type="policy")],
        )


@pytest.mark.parametrize("failure_type", ["policy", "dependency"])
def test_replan_rejects_policy_and_dependency_failures(failure_type: str):
    orchestrator = Orchestrator(max_replans=2)
    plan = Orchestrator().plan("开发一个系统并测试")

    with pytest.raises(PlanValidationError, match="human intervention"):
        orchestrator.replan(
            plan,
            [
                TaskResult(
                    task_id="implementation",
                    status="blocked",
                    failure_type=failure_type,
                    failure_reason=f"{failure_type} failure",
                    evidence={"cause": failure_type},
                )
            ],
        )


def test_delegate_backend_preserves_failure_reason_and_evidence():
    plan = Orchestrator().plan("比较三个方案并调研最新资料")

    response = {
        "results": [
            {
                "status": "needs_replan",
                "failure_type": "quality",
                "failure_reason": "missing citations",
                "evidence": {"artifacts": ["/tmp/report.md"], "notes": ["no source list"]},
            },
            {"status": "completed", "artifacts": ["/tmp/research-b.md"]},
            {
                "status": "blocked",
                "failure_type": "policy",
                "failure_reason": "needs human approval",
                "evidence": ["policy gate"],
            },
        ]
    }

    result = DelegateBackend().submit(plan, lambda **kwargs: json.dumps(response))

    assert result.success is False
    first, _, third = result.task_results
    assert first.status == "needs_replan"
    assert first.failure_reason == "missing citations"
    assert first.evidence == {"artifacts": ["/tmp/report.md"], "notes": ["no source list"]}
    assert third.status == "blocked"
    assert third.failure_reason == "needs human approval"
    assert third.evidence == ["policy gate"]
    assert result.raw_results[0]["failure_reason"] == "missing citations"


def test_plan_serializes_role_and_dependency_contract():
    payload = Orchestrator().plan("开发一个系统").to_dict()

    assert payload["execution_mode"] == "durable"
    implementation = next(item for item in payload["tasks"] if item["id"] == "implementation")
    assert implementation["depends_on"] == ["research", "architecture"]
    assert implementation["route"]["profile"] == "default"
    assert implementation["route"]["model_policy"] == "worker_fast"
    assert "terminal" in implementation["route"]["allowed_tools"]


def test_reviewer_uses_strong_review_policy():
    plan = Orchestrator().plan("开发一个系统并测试")

    reviewer = plan.task("review")

    assert reviewer.route is not None
    assert reviewer.route.profile == "default"
    assert reviewer.route.model_policy == "strong_review"


def test_execute_requires_explicit_backend():
    plan = Orchestrator().plan("比较三个方案并调研最新资料")

    with pytest.raises(PlanValidationError, match="execution backend"):
        Orchestrator().execute(plan)


def test_execute_delegates_to_injected_backend():
    plan = Orchestrator().plan("解释这个函数的作用")
    seen = []

    result = Orchestrator().execute(plan, executor=lambda value: seen.append(value.plan_id) or "ok")

    assert result == "ok"
    assert seen == [plan.plan_id]


def test_replan_limit_is_enforced():
    orchestrator = Orchestrator(max_replans=0)
    plan = Orchestrator().plan("开发一个系统")

    with pytest.raises(PlanValidationError, match="replan limit"):
        orchestrator.replan(
            plan,
            [TaskResult(task_id="implementation", status="failed", failure_type="quality")],
        )
