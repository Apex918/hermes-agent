"""Pure planning primitives for Hermes multi-agent orchestration.

This module deliberately does not spawn agents or mutate Kanban state. It
turns an intent into a validated plan and leaves execution to the existing
Hermes delegation and Kanban backends.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4


class PlanValidationError(ValueError):
    """Raised when a plan violates the orchestration contract."""


class ExecutionMode(str, Enum):
    DIRECT = "direct"
    FANOUT = "fanout"
    DURABLE = "durable"


class Label(str):
    """String label with the enum-like ``.value`` used by plugin consumers."""

    @property
    def value(self) -> str:
        return str(self)


@dataclass(frozen=True)
class Classification:
    domain: Tuple[Label, ...]
    complexity: str
    parallelism: str
    durability: str
    risk: str
    needs_review: bool
    confidence: float


@dataclass(frozen=True)
class RouteDecision:
    role: str
    profile: Optional[str]
    backend: str
    model_policy: str
    allowed_tools: Tuple[str, ...]


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    role: str
    backend: str
    depends_on: Tuple[str, ...] = ()
    status: str = "pending"
    acceptance_criteria: Tuple[str, ...] = ()
    route: Optional[RouteDecision] = None


@dataclass(frozen=True)
class Plan:
    plan_id: str
    objective: str
    execution_mode: ExecutionMode
    classification: Classification
    tasks: Tuple[TaskSpec, ...]
    replan_count: int = 0

    def task(self, task_id: str) -> TaskSpec:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    @property
    def subtasks(self) -> Tuple[TaskSpec, ...]:
        """Compatibility view for older plugin consumers."""
        return self.tasks

    @property
    def estimated_total_tokens(self) -> int:
        return sum(1200 if task.backend == "delegate_task" else 3000 for task in self.tasks)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "execution_mode": self.execution_mode.value,
            "classification": {
                "domain": [item.value for item in self.classification.domain],
                "complexity": self.classification.complexity,
                "parallelism": self.classification.parallelism,
                "durability": self.classification.durability,
                "risk": self.classification.risk,
                "needs_review": self.classification.needs_review,
                "confidence": self.classification.confidence,
            },
            "replan_count": self.replan_count,
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "role": task.role,
                    "backend": task.backend,
                    "depends_on": list(task.depends_on),
                    "status": task.status,
                    "acceptance_criteria": list(task.acceptance_criteria),
                    "route": {
                        "profile": task.route.profile,
                        "model_policy": task.route.model_policy,
                        "allowed_tools": list(task.route.allowed_tools),
                    }
                    if task.route
                    else None,
                }
                for task in self.tasks
            ],
        }


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: str
    failure_type: Optional[str] = None
    failure_reason: Optional[str] = None
    evidence: Any = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "failure_type": self.failure_type,
            "failure_reason": self.failure_reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TaskResult":
        failure_type = payload.get("failure_type")
        failure_reason = payload.get("failure_reason")
        evidence = payload.get("evidence")
        return cls(
            task_id=str(payload.get("task_id", "")),
            status=str(payload.get("status", "failed")),
            failure_type=str(failure_type) if failure_type is not None else None,
            failure_reason=(str(failure_reason) if failure_reason is not None else None),
            evidence=evidence,
        )


def _normalized_failure_type(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _normalized_result_status(value: str) -> str:
    return str(value or "").strip().lower()


class RoutePolicy:
    """Allowlisted role-to-backend routing policy.

    The planner may select a role, but it cannot invent a profile, model or
    toolset. Those decisions remain bounded by this registry.
    """

    _ROLES = {
        "coordinator": {
            "profile": None,
            "backend": "direct",
            "model_policy": "planner",
            "allowed_tools": ("file", "terminal", "todo"),
        },
        "researcher": {
            "profile": "default",
            "backend": "delegate_task",
            "model_policy": "worker_fast",
            "allowed_tools": ("web", "browser", "file"),
        },
        "architect": {
            "profile": "default",
            "backend": "delegate_task",
            "model_policy": "worker_fast",
            "allowed_tools": ("file", "terminal", "code_execution"),
        },
        "coder": {
            "profile": "default",
            "backend": "kanban",
            "model_policy": "worker_fast",
            "allowed_tools": ("file", "terminal", "code_execution"),
        },
        "tester": {
            "profile": "default",
            "backend": "kanban",
            "model_policy": "worker_fast",
            "allowed_tools": ("file", "terminal", "code_execution"),
        },
        "reviewer": {
            "profile": "default",
            "backend": "kanban",
            "model_policy": "strong_review",
            "allowed_tools": ("file", "terminal", "code_execution"),
        },
        "cto": {
            "profile": "cto",
            "backend": "kanban",
            "model_policy": "worker_fast",
            "allowed_tools": ("file", "terminal", "code_execution"),
        },
    }

    def route(self, role: str, requested_backend: Optional[str] = None) -> RouteDecision:
        spec = self._ROLES.get(role)
        if spec is None:
            raise PlanValidationError(f"unregistered role: {role}")
        backend = spec["backend"]
        if requested_backend is not None and requested_backend != backend:
            raise PlanValidationError(
                f"backend {requested_backend} is not allowed for role {role} (expected {backend})"
            )
        return RouteDecision(
            role=role,
            profile=spec["profile"],
            backend=backend,
            model_policy=spec["model_policy"],
            allowed_tools=spec["allowed_tools"],
        )


class Orchestrator:
    """Build and validate bounded plans without executing side effects."""

    _RECOVERABLE_FAILURES = frozenset(("transient", "quality", "capability"))

    def __init__(self, *, max_replans: int = 2, route_policy: Optional[RoutePolicy] = None):
        if max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        self.max_replans = max_replans
        self.route_policy = route_policy or RoutePolicy()

    def classify(self, objective: str) -> Classification:
        text = (objective or "").strip().lower()
        if not text:
            raise PlanValidationError("objective must not be empty")

        domains = []
        if any(word in text for word in ("研究", "调研", "research", "compare", "比较", "分析")):
            domains.append("research")
        if any(word in text for word in ("代码", "开发", "实现", "系统", "code", "build", "implement", "api")):
            domains.append("software")
        if any(word in text for word in ("金融", "股票", "投资", "行情", "finance", "market")):
            domains.append("finance")
        if any(word in text for word in ("自动化", "定时", "cron", "automation", "pipeline")):
            domains.append("automation")
        if any(word in text for word in ("内容", "文章", "报告", "文档", "content", "document")):
            domains.append("content")

        asks_for_testing = any(word in text for word in ("测试", "验证", "test", "verify", "审查", "review"))
        asks_for_build = "software" in domains
        asks_for_research = "research" in domains

        if asks_for_build:
            complexity = "complex"
            parallelism = "partial"
            durability = "durable"
            risk = "high" if asks_for_testing or len(domains) > 1 else "medium"
            needs_review = True
        elif asks_for_research and any(word in text for word in ("比较", "compare", "多个", "multiple", "最新")):
            complexity = "parallel"
            parallelism = "high"
            durability = "ephemeral"
            risk = "low"
            needs_review = False
        else:
            complexity = "simple"
            parallelism = "none"
            durability = "ephemeral"
            risk = "low"
            needs_review = False

        return Classification(
            domain=tuple(Label(item) for item in (domains or ("general",))),
            complexity=complexity,
            parallelism=parallelism,
            durability=durability,
            risk=risk,
            needs_review=needs_review,
            confidence=0.85 if complexity != "simple" else 0.9,
        )

    def plan(self, objective: str, *, plan_id: Optional[str] = None) -> Plan:
        classification = self.classify(objective)
        pid = plan_id or "plan_" + uuid4().hex[:12]
        if classification.complexity == "simple":
            tasks = (self._task("direct", objective, "coordinator"),)
            mode = ExecutionMode.DIRECT
        elif classification.complexity == "parallel":
            tasks = (
                self._task("research_a", "调查方案 A", "researcher"),
                self._task("research_b", "调查方案 B", "researcher"),
                self._task("analysis", "独立分析比较维度和风险", "researcher"),
            )
            mode = ExecutionMode.FANOUT
        else:
            tasks = (
                self._task("research", "研究输入、约束和可用数据源", "researcher"),
                self._task("architecture", "设计实现架构和验收标准", "architect"),
                self._task(
                    "implementation",
                    "实现目标功能",
                    "coder",
                    ("research", "architecture"),
                    ("遵守项目约定", "使用 TDD", "保留实际测试结果"),
                ),
                self._task(
                    "testing",
                    "运行测试并补齐回归验证",
                    "tester",
                    ("implementation",),
                    ("测试必须真实执行", "报告命令和返回结果"),
                ),
                self._task(
                    "review",
                    "独立审查实现、测试和风险",
                    "reviewer",
                    ("testing",),
                    ("重新读取实际 diff", "明确通过或要求修改"),
                ),
            )
            mode = ExecutionMode.DURABLE

        plan = Plan(pid, objective.strip(), mode, classification, tuple(tasks))
        self.validate_plan(plan)
        return plan

    def plan_from_mapping(self, raw: Mapping[str, object]) -> Plan:
        """Normalize and validate a plan returned by an external Planner model.

        The model may choose task wording and dependencies, but role, backend,
        and tool permissions remain constrained by ``RoutePolicy``. Missing
        classification metadata is derived locally from the objective.
        """
        if not isinstance(raw, Mapping):
            raise PlanValidationError("planner output must be an object")
        required = ("plan_id", "objective", "execution_mode", "tasks")
        missing = [key for key in required if key not in raw]
        if missing:
            raise PlanValidationError("missing plan fields: %s" % ", ".join(missing))
        try:
            execution_mode = ExecutionMode(str(raw["execution_mode"]))
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("invalid execution_mode") from exc
        objective = str(raw["objective"]).strip()
        plan_id = str(raw["plan_id"]).strip()
        raw_tasks = raw["tasks"]
        if not objective or not plan_id or not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
            raise PlanValidationError("plan_id, objective, and tasks are required")

        classification = self.classify(objective)
        raw_classification = raw.get("classification")
        if isinstance(raw_classification, Mapping):
            domains = raw_classification.get("domain", classification.domain)
            if isinstance(domains, str):
                domains = [domains]
            classification = Classification(
                domain=tuple(Label(str(item)) for item in domains),
                complexity=str(raw_classification.get("complexity", classification.complexity)),
                parallelism=str(raw_classification.get("parallelism", classification.parallelism)),
                durability=str(raw_classification.get("durability", classification.durability)),
                risk=str(raw_classification.get("risk", classification.risk)),
                needs_review=bool(raw_classification.get("needs_review", classification.needs_review)),
                confidence=float(raw_classification.get("confidence", classification.confidence)),
            )

        tasks = []
        for item in raw_tasks:
            if not isinstance(item, Mapping):
                raise PlanValidationError("every task must be an object")
            task_id = str(item.get("id", "")).strip()
            role = str(item.get("role", "")).strip()
            if not task_id or not role:
                raise PlanValidationError("task id and role are required")
            requested_backend = item.get("backend")
            route = self.route_policy.route(
                role, str(requested_backend) if requested_backend is not None else None
            )
            dependencies = item.get("depends_on", ())
            if isinstance(dependencies, str):
                dependencies = (dependencies,)
            if not isinstance(dependencies, (list, tuple)):
                raise PlanValidationError("task depends_on must be a list")
            criteria = item.get("acceptance_criteria", ())
            if isinstance(criteria, str):
                criteria = (criteria,)
            tasks.append(
                TaskSpec(
                    id=task_id,
                    title=str(item.get("title", task_id)),
                    role=role,
                    backend=route.backend,
                    depends_on=tuple(str(value) for value in dependencies),
                    status=str(item.get("status", "ready" if not dependencies else "pending")),
                    acceptance_criteria=tuple(str(value) for value in criteria),
                    route=route,
                )
            )

        plan = Plan(
            plan_id=plan_id,
            objective=objective,
            execution_mode=execution_mode,
            classification=classification,
            tasks=tuple(tasks),
            replan_count=int(str(raw.get("replan_count", 0) or 0)),
        )
        self.validate_plan(plan)
        return plan

    def validate_plan(self, plan: object) -> Plan:
        if isinstance(plan, Plan):
            tasks = plan.tasks
        elif isinstance(plan, Mapping):
            tasks = tuple(
                TaskSpec(
                    id=item["id"],
                    title=item.get("title", item["id"]),
                    role=item["role"],
                    backend=item.get("backend", self.route_policy.route(item["role"]).backend),
                    depends_on=tuple(item.get("depends_on", ())),
                )
                for item in plan.get("tasks", ())
            )
        else:
            raise PlanValidationError("plan must be a Plan or mapping")

        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("duplicate task id")
        known = set(ids)
        for task in tasks:
            self.route_policy.route(task.role, task.backend)
            for dependency in task.depends_on:
                if dependency not in known:
                    raise PlanValidationError("unknown dependency: %s" % dependency)

        visiting = set()
        visited = set()
        by_id = {task.id: task for task in tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise PlanValidationError("cycle detected")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)
        return plan if isinstance(plan, Plan) else plan  # type: ignore[return-value]

    def replan(self, plan: Plan, results: Sequence[TaskResult]) -> Plan:
        self.validate_plan(plan)
        for result in results:
            failure_type = _normalized_failure_type(result.failure_type)
            status = _normalized_result_status(result.status)
            if failure_type in {"policy", "dependency"} or status == "blocked":
                raise PlanValidationError(
                    f"{failure_type or status} failure requires human intervention"
                )
        if plan.replan_count >= self.max_replans:
            raise PlanValidationError("replan limit reached")
        by_id = {task.id: task for task in plan.tasks}
        changed = False
        for result in results:
            failure_type = _normalized_failure_type(result.failure_type)
            status = _normalized_result_status(result.status)
            recoverable = status == "needs_replan" or failure_type in self._RECOVERABLE_FAILURES
            if not recoverable:
                continue
            task = by_id.get(result.task_id)
            if task is None:
                raise PlanValidationError("unknown failed task: %s" % result.task_id)
            by_id[result.task_id] = replace(task, status="ready")
            changed = True

        if not changed:
            raise PlanValidationError("no recoverable failure to replan")
        if "review" in by_id:
            by_id["review"] = replace(by_id["review"], status="pending")
        replanned = replace(
            plan,
            tasks=tuple(by_id[task.id] for task in plan.tasks),
            replan_count=plan.replan_count + 1,
        )
        self.validate_plan(replanned)
        return replanned

    def execute(self, plan: Plan, executor=None):
        """Execute through an injected backend, never through hidden side effects.

        The production adapters will inject Hermes' existing delegation or
        Kanban backend.  Requiring the executor explicitly prevents a plugin
        hook from silently creating agents before policy/approval is applied.
        """
        self.validate_plan(plan)
        if executor is None:
            raise PlanValidationError(
                "execution backend is not configured; use delegate_task or Kanban adapter"
            )
        return executor(plan)

    def _task(
        self,
        task_id: str,
        title: str,
        role: str,
        depends_on: Iterable[str] = (),
        acceptance_criteria: Iterable[str] = (),
    ) -> TaskSpec:
        route = self.route_policy.route(role)
        status = "ready" if not tuple(depends_on) else "pending"
        return TaskSpec(
            id=task_id,
            title=title,
            role=role,
            backend=route.backend,
            depends_on=tuple(depends_on),
            status=status,
            acceptance_criteria=tuple(acceptance_criteria),
            route=route,
        )


__all__ = [
    "Classification",
    "ExecutionMode",
    "Label",
    "Orchestrator",
    "Plan",
    "PlanValidationError",
    "RouteDecision",
    "RoutePolicy",
    "TaskResult",
    "TaskSpec",
]
