"""Bounded orchestration runtime that composes existing Hermes backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agent.orchestration_backends import DelegateBackend, DelegateExecutionResult, KanbanBackend
from agent.orchestrator import ExecutionMode, Plan, PlanValidationError, TaskResult


@dataclass(frozen=True)
class OrchestrationRunResult:
    success: bool
    task_results: tuple[TaskResult, ...]
    preflight: Optional[DelegateExecutionResult] = None
    kanban_tasks: Optional[dict[str, str]] = None
    graph: Optional[dict[str, Any]] = None
    receipt_id: str = ""
    phase: str = "done"
    resumed: bool = False
    resume_count: int = 0
    completed: bool = False

    def __post_init__(self):
        if self.kanban_tasks is None:
            object.__setattr__(self, "kanban_tasks", {})
        if self.graph is None:
            object.__setattr__(self, "graph", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "task_results": [item.to_dict() for item in self.task_results],
            "preflight": self.preflight.to_dict() if self.preflight else None,
            "kanban_tasks": dict(self.kanban_tasks or {}),
            "graph": dict(self.graph or {}),
            "receipt_id": self.receipt_id,
            "phase": self.phase,
            "resumed": self.resumed,
            "resume_count": self.resume_count,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OrchestrationRunResult":
        preflight = payload.get("preflight") or None
        if isinstance(preflight, dict):
            preflight = DelegateExecutionResult.from_dict(preflight)
        return cls(
            success=bool(payload.get("success", False)),
            task_results=tuple(
                TaskResult.from_dict(item) for item in payload.get("task_results", [])
            ),
            preflight=preflight,
            kanban_tasks=dict(payload.get("kanban_tasks") or {}),
            graph=dict(payload.get("graph") or {}),
            receipt_id=str(payload.get("receipt_id", "")),
            phase=str(payload.get("phase", "done")),
            resumed=bool(payload.get("resumed", False)),
            resume_count=int(payload.get("resume_count", 0) or 0),
            completed=bool(payload.get("completed", False)),
        )


class OrchestrationRuntime:
    """Compose delegate fan-out and Kanban submission with a hard gate."""

    def __init__(self, *, delegate_backend=None, kanban_backend=None):
        self.delegate_backend = delegate_backend or DelegateBackend()
        self.kanban_backend = kanban_backend or KanbanBackend()

    def build_graph(self, plan: Plan) -> dict[str, Any]:
        graph = self.kanban_backend.compile(plan)
        graph["nodes"] = list(graph.get("tasks", ()))
        graph["edges"] = list(graph.get("links", ()))
        graph["preflight_task_ids"] = [
            task.id for task in plan.tasks if task.backend == "delegate_task"
        ]
        return graph

    def preflight(self, plan: Plan, *, delegate_fn=None, parent_agent=None):
        if plan.execution_mode is ExecutionMode.FANOUT:
            if delegate_fn is None:
                raise PlanValidationError("delegate_fn is required for fanout execution")
            result = self.delegate_backend.submit(
                plan, delegate_fn, parent_agent=parent_agent
            )
            return OrchestrationRunResult(
                success=result.success,
                task_results=result.task_results,
                preflight=result,
                graph=self.build_graph(plan),
                phase="done",
                completed=True,
            )

        if plan.execution_mode is not ExecutionMode.DURABLE:
            raise PlanValidationError("direct plans must remain in the Main Agent loop")
        if delegate_fn is None:
            raise PlanValidationError("delegate_fn is required for durable preflight")

        preflight_tasks = tuple(
            task for task in plan.tasks if task.backend == "delegate_task"
        )
        if not preflight_tasks:
            raise PlanValidationError("durable plan has no delegate preflight tasks")
        preflight_plan = Plan(
            plan_id=plan.plan_id + "/preflight",
            objective=plan.objective,
            execution_mode=ExecutionMode.FANOUT,
            classification=plan.classification,
            tasks=preflight_tasks,
        )
        preflight = self.delegate_backend.submit(
            preflight_plan, delegate_fn, parent_agent=parent_agent
        )
        return OrchestrationRunResult(
            success=preflight.success,
            task_results=preflight.task_results,
            preflight=preflight,
            graph=self.build_graph(plan),
            phase="awaiting_kanban" if preflight.success else "failed",
            completed=False,
        )

    def commit(
        self,
        plan: Plan,
        preflight: DelegateExecutionResult,
        *,
        kanban_conn=None,
        created_by: str = "orchestrator",
        tenant: Optional[str] = None,
    ):
        if plan.execution_mode is not ExecutionMode.DURABLE:
            raise PlanValidationError("only durable plans can be committed to Kanban")
        if kanban_conn is None:
            raise PlanValidationError("kanban_conn is required for durable execution")
        if not preflight.success:
            return OrchestrationRunResult(
                success=False,
                task_results=preflight.task_results,
                preflight=preflight,
                graph=self.build_graph(plan),
                phase="failed",
                completed=False,
            )

        kanban_tasks = self.kanban_backend.submit(
            plan,
            kanban_conn,
            preflight_artifacts=preflight.artifacts_by_task,
            created_by=created_by,
            tenant=tenant,
        )
        return OrchestrationRunResult(
            success=True,
            task_results=preflight.task_results,
            preflight=preflight,
            kanban_tasks=kanban_tasks,
            graph=self.build_graph(plan),
            phase="done",
            resumed=True,
            resume_count=1,
            completed=True,
        )

    def run(self, plan: Plan, *, delegate_fn=None, parent_agent=None, kanban_conn=None):
        preflight = self.preflight(plan, delegate_fn=delegate_fn, parent_agent=parent_agent)
        if plan.execution_mode is ExecutionMode.FANOUT:
            return preflight
        if not preflight.success or preflight.preflight is None:
            return preflight
        return self.commit(
            plan,
            preflight.preflight,
            kanban_conn=kanban_conn,
            created_by="orchestrator",
        )


__all__ = ["OrchestrationRunResult", "OrchestrationRuntime"]
