"""Compilers and explicit adapters for Hermes orchestration backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.model_routing import resolve_model_routes
from agent.orchestrator import (
    ExecutionMode,
    Orchestrator,
    Plan,
    PlanValidationError,
    TaskResult,
    require_total_time_budget,
)


@dataclass(frozen=True)
class DelegateExecutionResult:
    success: bool
    task_results: tuple[TaskResult, ...]
    artifacts: tuple[str, ...]
    raw_results: tuple[dict, ...]

    @property
    def artifacts_by_task(self) -> Dict[str, tuple[str, ...]]:
        return {
            item["task_id"]: tuple(item.get("artifacts", ()) or ())
            for item in self.raw_results
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "task_results": [item.to_dict() for item in self.task_results],
            "artifacts": list(self.artifacts),
            "raw_results": [dict(item) for item in self.raw_results],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DelegateExecutionResult":
        return cls(
            success=bool(payload.get("success", False)),
            task_results=tuple(
                TaskResult.from_dict(item) for item in payload.get("task_results", [])
            ),
            artifacts=tuple(str(item) for item in payload.get("artifacts", []) if item),
            raw_results=tuple(
                dict(item)
                for item in payload.get("raw_results", [])
                if isinstance(item, dict)
            ),
        )


class DelegateBackend:
    """Compile a fan-out plan into model-facing delegate_task arguments."""

    def compile(self, plan: Plan) -> List[dict]:
        if plan.execution_mode is not ExecutionMode.FANOUT:
            raise PlanValidationError(
                "only independent delegate_task plans can be compiled by DelegateBackend"
            )
        if any(task.depends_on for task in plan.tasks):
            raise PlanValidationError("delegate_task batch cannot contain dependencies")
        return [
            {
                "task_id": task.id,
                "backend": "delegate_task",
                "goal": task.title,
                "context": self._context(plan, task),
            }
            for task in plan.tasks
        ]

    def submit(self, plan: Plan, delegate_fn, *, parent_agent=None) -> DelegateExecutionResult:
        """Submit a fan-out batch through the already-bound Hermes callable.

        ``delegate_fn`` is intentionally injected by the Main Agent runtime;
        this adapter never imports or fabricates a parent agent.  The callable
        must be the existing ``delegate_task`` implementation and return its
        normal JSON result envelope.
        """
        batch = self.compile(plan)
        raw = delegate_fn(tasks=batch, parent_agent=parent_agent)
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("delegate_task result must contain JSON results") from exc
        elif isinstance(raw, dict):
            payload = raw
        else:
            raise ValueError("delegate_task result must contain JSON results")
        entries = payload.get("results")
        if not isinstance(entries, list) or len(entries) != len(batch):
            raise ValueError("delegate_task result must contain one results entry per task")

        task_results = []
        artifacts = []
        normalized = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError("delegate_task result entries must be objects")
            task_id = batch[index]["task_id"]
            status = str(entry.get("status", "failed")).lower()
            failure_type = entry.get("failure_type")
            failure_reason = entry.get("failure_reason")
            evidence = entry.get("evidence")
            if status not in {"completed", "success", "done"} and not failure_type:
                failure_type = "unknown"
            task_results.append(
                TaskResult(
                    task_id,
                    status,
                    failure_type,
                    failure_reason=(
                        str(failure_reason) if failure_reason is not None else None
                    ),
                    evidence=evidence,
                )
            )
            for artifact in entry.get("artifacts", ()) or ():
                if isinstance(artifact, str) and artifact:
                    artifacts.append(artifact)
            normalized.append(dict(entry, task_id=task_id))
        return DelegateExecutionResult(
            success=all(item.status in {"completed", "success", "done"} for item in task_results),
            task_results=tuple(task_results),
            artifacts=tuple(artifacts),
            raw_results=tuple(normalized),
        )

    @staticmethod
    def _context(plan: Plan, task) -> str:
        criteria = "\n".join("- " + item for item in task.acceptance_criteria) or "- Return structured findings and evidence"
        return (
            "task_id: %s\n"
            "parent_objective: %s\n"
            "role: %s\n"
            "acceptance_criteria:\n%s\n"
            "Do not modify shared project state or perform external side effects."
            % (task.id, plan.objective, task.role, criteria)
        )


class KanbanBackend:
    """Compile a durable plan into Kanban DAG writes."""

    def compile(self, plan: Plan) -> dict:
        if plan.execution_mode is not ExecutionMode.DURABLE:
            raise PlanValidationError("only durable plans can be compiled as Kanban DAG")
        # Compilation is a side-effect-free preview used by the plugin UI.
        # The runtime/submit gates enforce that durable execution has a budget.
        total_time_budget_seconds = plan.total_time_budget_seconds
        tasks = []
        links = []
        for task in plan.tasks:
            tasks.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "assignee": task.route.profile if task.route else task.role,
                    "body": self._body(plan, task),
                    "workspace": "worktree" if task.backend == "kanban" else "scratch",
                    "status": task.status,
                    "max_runtime_seconds": total_time_budget_seconds,
                }
            )
            links.extend([[dependency, task.id] for dependency in task.depends_on])
        return {
            "plan_id": plan.plan_id,
            "total_time_budget_seconds": total_time_budget_seconds,
            "tasks": tasks,
            "links": links,
        }

    def submit(
        self,
        plan: Plan,
        conn,
        *,
        preflight_artifacts=None,
        created_by: str = "orchestrator",
        tenant: Optional[str] = None,
    ) -> Dict[str, str]:
        """Submit only the durable portion after verified preflight artifacts."""
        if plan.execution_mode is not ExecutionMode.DURABLE:
            raise PlanValidationError("only durable plans can be submitted to Kanban")
        total_time_budget_seconds = require_total_time_budget(plan)
        compiled = self.compile(plan)
        artifacts = preflight_artifacts or {}
        missing = [
            task.id
            for task in plan.tasks
            if task.backend == "delegate_task" and not artifacts.get(task.id)
        ]
        if missing:
            raise ValueError("preflight artifacts required for: %s" % ", ".join(missing))

        from hermes_cli import kanban_db as kb

        created = {}
        kanban_tasks = [task for task in plan.tasks if task.backend == "kanban"]
        for task in kanban_tasks:
            policy = task.route.model_policy if task.route else "worker_fast"
            target = resolve_model_routes().for_policy(policy).primary
            body = self._body(plan, task)
            gate_lines = []
            for parent in task.depends_on:
                if parent in artifacts:
                    gate_lines.append("- %s: %s" % (parent, ", ".join(artifacts[parent])))
            if gate_lines:
                body += "\n\nVerified preflight artifacts:\n" + "\n".join(gate_lines)
            body += (
                "\n\nScheduler-enforced total time budget: %s seconds."
                % total_time_budget_seconds
            )
            created[task.id] = kb.create_task(
                conn,
                title=task.title,
                body=body,
                assignee=task.route.profile if task.route else task.role,
                created_by=created_by,
                parents=(),
                tenant=tenant,
                workspace_kind="worktree",
                max_runtime_seconds=total_time_budget_seconds,
                skills=list(task.acceptance_criteria),
                idempotency_key="%s/%s" % (plan.plan_id, task.id),
                model_override=target.model,
                provider_override=target.provider,
                initial_status="running",
            )

        for parent, child in compiled["links"]:
            if parent in created and child in created:
                kb.link_tasks(conn, created[parent], created[child])
        return created

    @staticmethod
    def _body(plan: Plan, task) -> str:
        criteria = "\n".join("- " + item for item in task.acceptance_criteria) or "- Return a structured handoff with evidence"
        return (
            "Parent objective: %s\n"
            "Task id: %s\n"
            "Role: %s\n"
            "Acceptance criteria:\n%s\n"
            "Do not close the task without real artifact paths and verification results."
            % (plan.objective, task.id, task.role, criteria)
        )


__all__ = ["DelegateBackend", "KanbanBackend", "DelegateExecutionResult"]
