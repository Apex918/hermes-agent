"""Workflow adapter proof for plan → graph → receipt flows.

This is a small orchestration wrapper around the existing planner,
preflight delegate fan-out, and Kanban submission backends. It keeps the
workflow explicit and serializable so a kill/restart can resume the durable
portion exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from uuid import uuid4

from agent.orchestration_runtime import OrchestrationRunResult, OrchestrationRuntime
from agent.orchestration_backends import DelegateExecutionResult
from agent.orchestrator import ExecutionMode, Orchestrator, Plan, PlanValidationError


@dataclass
class WorkflowReceipt:
    receipt_id: str
    plan: dict[str, Any]
    graph: dict[str, Any]
    preflight: Optional[DelegateExecutionResult] = None
    final_result: Optional[OrchestrationRunResult] = None
    phase: str = "awaiting_kanban"
    completed: bool = False
    resume_count: int = 0
    kanban_tasks: dict[str, str] = field(default_factory=dict)
    task_results: tuple[dict[str, Any], ...] = ()
    event_log: tuple[str, ...] = ()

    @property
    def event_receipt(self) -> dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "plan": self.plan,
            "graph": self.graph,
            "preflight": self.preflight.to_dict() if self.preflight else None,
            "final_result": self.final_result.to_dict() if self.final_result else None,
            "phase": self.phase,
            "completed": self.completed,
            "resume_count": self.resume_count,
            "kanban_tasks": dict(self.kanban_tasks),
            "task_results": [dict(item) for item in self.task_results],
            "event_log": list(self.event_log),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkflowReceipt":
        preflight = payload.get("preflight")
        if isinstance(preflight, dict):
            preflight = DelegateExecutionResult.from_dict(preflight)
        final_result = payload.get("final_result")
        if isinstance(final_result, dict):
            final_result = OrchestrationRunResult.from_dict(final_result)
        return cls(
            receipt_id=str(payload.get("receipt_id", "")),
            plan=dict(payload.get("plan") or {}),
            graph=dict(payload.get("graph") or {}),
            preflight=preflight,
            final_result=final_result,
            phase=str(payload.get("phase", "awaiting_kanban")),
            completed=bool(payload.get("completed", False)),
            resume_count=int(payload.get("resume_count", 0) or 0),
            kanban_tasks=dict(payload.get("kanban_tasks") or {}),
            task_results=tuple(
                dict(item) for item in payload.get("task_results", []) if isinstance(item, dict)
            ),
            event_log=tuple(
                str(item) for item in payload.get("event_log", []) if item is not None
            ),
        )


class WorkflowAdapter:
    """Proof adapter that makes the orchestration graph resumable once."""

    def __init__(self, *, runtime: Optional[OrchestrationRuntime] = None, orchestrator: Optional[Orchestrator] = None):
        self.runtime = runtime or OrchestrationRuntime()
        self.orchestrator = orchestrator or Orchestrator()
        self._receipts: dict[str, WorkflowReceipt] = {}

    def to_graph(self, plan: Plan | Mapping[str, Any] | str) -> dict[str, Any]:
        plan_obj = self._coerce_plan(plan)
        return self.runtime.build_graph(plan_obj)

    def start(
        self,
        plan_or_objective: Plan | Mapping[str, Any] | str,
        *,
        delegate_fn=None,
        parent_agent=None,
    ) -> WorkflowReceipt:
        plan = self._coerce_plan(plan_or_objective)
        receipt_id = "receipt_" + uuid4().hex[:12]
        graph = self.to_graph(plan)
        preflight = self.runtime.preflight(plan, delegate_fn=delegate_fn, parent_agent=parent_agent)

        if plan.execution_mode is ExecutionMode.DURABLE and preflight.success:
            receipt = WorkflowReceipt(
                receipt_id=receipt_id,
                plan=plan.to_dict(),
                graph=graph,
                preflight=preflight.preflight,
                phase="awaiting_kanban",
                completed=False,
                task_results=tuple(item.to_dict() for item in preflight.task_results),
                event_log=("preflight",),
            )
            self._receipts[receipt.receipt_id] = receipt
            return receipt

        final_result = preflight if plan.execution_mode is ExecutionMode.FANOUT else preflight
        if plan.execution_mode is ExecutionMode.DURABLE and not preflight.success:
            receipt = WorkflowReceipt(
                receipt_id=receipt_id,
                plan=plan.to_dict(),
                graph=graph,
                preflight=preflight.preflight,
                final_result=final_result,
                phase="failed",
                completed=True,
                resume_count=0,
                task_results=tuple(item.to_dict() for item in preflight.task_results),
                event_log=("preflight_failed",),
            )
            self._receipts[receipt.receipt_id] = receipt
            return receipt

        receipt = WorkflowReceipt(
            receipt_id=receipt_id,
            plan=plan.to_dict(),
            graph=graph,
            preflight=preflight.preflight,
            final_result=final_result,
            phase="done",
            completed=True,
            resume_count=0,
            kanban_tasks=dict(final_result.kanban_tasks or {}),
            task_results=tuple(item.to_dict() for item in final_result.task_results),
            event_log=("completed",),
        )
        self._receipts[receipt.receipt_id] = receipt
        return receipt

    def resume(
        self,
        receipt: WorkflowReceipt | Mapping[str, Any] | str,
        *,
        kanban_conn=None,
        created_by: str = "orchestrator",
        tenant: Optional[str] = None,
    ) -> OrchestrationRunResult:
        receipt_obj = self._coerce_receipt(receipt)
        if receipt_obj.completed:
            if receipt_obj.final_result is not None:
                return receipt_obj.final_result
            return OrchestrationRunResult.from_dict(
                {
                    "success": receipt_obj.phase != "failed",
                    "task_results": receipt_obj.task_results,
                    "preflight": receipt_obj.preflight.to_dict() if receipt_obj.preflight else None,
                    "kanban_tasks": receipt_obj.kanban_tasks,
                    "graph": receipt_obj.graph,
                    "receipt_id": receipt_obj.receipt_id,
                    "phase": receipt_obj.phase,
                    "resumed": receipt_obj.resume_count > 0,
                    "resume_count": receipt_obj.resume_count,
                    "completed": receipt_obj.completed,
                }
            )

        plan = self.orchestrator.plan_from_mapping(receipt_obj.plan)
        if receipt_obj.preflight is None:
            raise PlanValidationError("workflow receipt is missing preflight state")
        if receipt_obj.resume_count >= 1:
            if receipt_obj.final_result is not None:
                return receipt_obj.final_result
            raise PlanValidationError("workflow receipt can only resume once")

        final_result = self.runtime.commit(
            plan,
            receipt_obj.preflight,
            kanban_conn=kanban_conn,
            created_by=created_by,
            tenant=tenant,
        )
        receipt_obj.resume_count += 1
        receipt_obj.completed = True
        receipt_obj.phase = final_result.phase
        receipt_obj.kanban_tasks = dict(final_result.kanban_tasks or {})
        receipt_obj.task_results = tuple(item.to_dict() for item in final_result.task_results)
        receipt_obj.final_result = final_result
        receipt_obj.event_log = receipt_obj.event_log + ("resumed",)
        self._receipts[receipt_obj.receipt_id] = receipt_obj
        return final_result

    def run(
        self,
        plan_or_objective: Plan | Mapping[str, Any] | str,
        *,
        delegate_fn=None,
        parent_agent=None,
        kanban_conn=None,
        receipt: WorkflowReceipt | Mapping[str, Any] | str | None = None,
        interrupt_after: Optional[str] = None,
        created_by: str = "orchestrator",
        tenant: Optional[str] = None,
    ):
        if receipt is not None:
            return self.resume(
                receipt,
                kanban_conn=kanban_conn,
                created_by=created_by,
                tenant=tenant,
            )

        receipt_obj = self.start(
            plan_or_objective,
            delegate_fn=delegate_fn,
            parent_agent=parent_agent,
        )
        if interrupt_after == "preflight":
            return receipt_obj
        if receipt_obj.completed:
            return receipt_obj.final_result or self.resume(
                receipt_obj,
                kanban_conn=kanban_conn,
                created_by=created_by,
                tenant=tenant,
            )
        return self.resume(
            receipt_obj,
            kanban_conn=kanban_conn,
            created_by=created_by,
            tenant=tenant,
        )

    def _coerce_plan(self, plan_or_objective: Plan | Mapping[str, Any] | str) -> Plan:
        if isinstance(plan_or_objective, Plan):
            return plan_or_objective
        if isinstance(plan_or_objective, str):
            return self.orchestrator.plan(plan_or_objective)
        return self.orchestrator.plan_from_mapping(plan_or_objective)

    def _coerce_receipt(
        self, receipt: WorkflowReceipt | Mapping[str, Any] | str
    ) -> WorkflowReceipt:
        if isinstance(receipt, WorkflowReceipt):
            self._receipts[receipt.receipt_id] = receipt
            return receipt
        if isinstance(receipt, str):
            cached = self._receipts.get(receipt)
            if cached is not None:
                return cached
            raise PlanValidationError(f"unknown workflow receipt: {receipt}")
        receipt_obj = WorkflowReceipt.from_dict(receipt)
        if not receipt_obj.receipt_id:
            raise PlanValidationError("workflow receipt is missing receipt_id")
        self._receipts[receipt_obj.receipt_id] = receipt_obj
        return receipt_obj


__all__ = ["WorkflowAdapter", "WorkflowReceipt"]
