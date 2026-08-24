import pytest

from agent.orchestrator import Orchestrator, PlanValidationError, RoutePolicy


def test_cto_role_routes_to_business_agent_profile_and_read_only_backend():
    route = RoutePolicy().route("cto")

    assert route.role == "cto"
    assert route.profile == "cto"
    assert route.backend == "kanban"
    assert route.model_policy == "worker_fast"
    assert route.allowed_tools == ("file", "terminal", "code_execution")


def test_planner_mapping_accepts_cto_role_but_rejects_delegate_backend():
    orchestrator = Orchestrator()
    plan = orchestrator.plan_from_mapping(
        {
            "plan_id": "cto-plan",
            "objective": "审查仓库架构和 CI 状态",
            "execution_mode": "durable",
            "tasks": [
                {
                    "id": "cto_scan",
                    "title": "运行 CTO 只读技术状态扫描",
                    "role": "cto",
                    "backend": "kanban",
                    "depends_on": [],
                }
            ],
        }
    )

    task = plan.task("cto_scan")
    assert task.route is not None
    assert task.route.profile == "cto"
    assert task.route.backend == "kanban"

    with pytest.raises(PlanValidationError, match="not allowed"):
        orchestrator.plan_from_mapping(
            {
                "plan_id": "bad-cto-plan",
                "objective": "审查架构",
                "execution_mode": "fanout",
                "tasks": [
                    {
                        "id": "cto_scan",
                        "title": "错误的 CTO delegate 路由",
                        "role": "cto",
                        "backend": "delegate_task",
                    }
                ],
            }
        )
