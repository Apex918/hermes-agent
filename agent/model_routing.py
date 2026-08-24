"""Model-role routing for the Planner → Worker orchestration boundary.

The router is intentionally small and provider-agnostic. It resolves role
configuration, calls a plan-only model, validates the returned plan through the
existing orchestration core, and reports the model actually used after
fallback. Execution remains delegated to Hermes' existing delegate/Kanban
backends.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


_DEFAULT_PLANNER = {"provider": "xai-oauth", "model": "grok-4.6"}
_DEFAULT_PLANNER_FALLBACKS = [
    {"provider": "hp-fenno", "model": "gpt-5.6-sol"},
]
_DEFAULT_WORKER = {"provider": "hp-fenno", "model": "gpt-5.6-luna"}


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    base_url: str = ""
    api_mode: str = ""

    def as_call_kwargs(self) -> Dict[str, Any]:
        result = {"provider": self.provider, "model": self.model}
        if self.base_url:
            result["base_url"] = self.base_url
        if self.api_mode:
            result["api_mode"] = self.api_mode
        return result


@dataclass(frozen=True)
class RoleRoute:
    primary: ModelTarget
    fallbacks: tuple[ModelTarget, ...] = ()

    def candidates(self) -> tuple[ModelTarget, ...]:
        return (self.primary,) + self.fallbacks


@dataclass(frozen=True)
class ModelRoutes:
    planner: RoleRoute
    worker: RoleRoute
    reviewer: RoleRoute

    def for_policy(self, policy: str) -> RoleRoute:
        if policy in {"planner", "frontier", "strong_review"}:
            return self.reviewer if policy == "strong_review" else self.planner
        if policy in {"worker", "worker_fast", "coding", "research", "verification"}:
            return self.worker
        raise ValueError("unknown model policy: %s" % policy)


@dataclass(frozen=True)
class PlannerResult:
    plan: Any
    actual_provider: str
    actual_model: str
    fallback_used: bool
    attempted: tuple[ModelTarget, ...]


def _target(raw: Any, default: Mapping[str, str]) -> ModelTarget:
    if isinstance(raw, str):
        provider, sep, model = raw.partition("/")
        raw = {"provider": provider, "model": model if sep else provider}
    if not isinstance(raw, Mapping):
        raw = {}
    provider = str(raw.get("provider") or default.get("provider") or "").strip()
    model = str(raw.get("model") or default.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("model route requires both provider and model")
    return ModelTarget(
        provider=provider,
        model=model,
        base_url=str(raw.get("base_url") or "").strip(),
        api_mode=str(raw.get("api_mode") or "").strip(),
    )


def _route(raw: Any, primary_default: Mapping[str, str], fallback_defaults: Sequence[Mapping[str, str]]) -> RoleRoute:
    raw = raw if isinstance(raw, Mapping) else {}
    primary = _target(raw.get("primary"), primary_default)
    fallback_raw = raw.get("fallback", raw.get("fallbacks"))
    if fallback_raw is None:
        fallback_raw = fallback_defaults
    if isinstance(fallback_raw, Mapping) or isinstance(fallback_raw, str):
        fallback_raw = [fallback_raw]
    fallbacks = tuple(
        _target(item, fallback_defaults[index] if index < len(fallback_defaults) else {})
        for index, item in enumerate(fallback_raw or ())
    )
    # Avoid making the same endpoint/model a pointless fallback candidate.
    fallbacks = tuple(item for item in fallbacks if item != primary)
    return RoleRoute(primary=primary, fallbacks=fallbacks)


def resolve_model_routes(config: Optional[Mapping[str, Any]] = None) -> ModelRoutes:
    """Resolve planner/worker/reviewer routes from config or safe defaults.

    ``orchestration`` is a new explicit section. The function accepts a config
    mapping so tests and callers can resolve profile-scoped config without
    touching global state. With no mapping it reads the active Hermes config.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception:
            config = {}
    raw = config.get("orchestration", {}) if isinstance(config, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    planner = _route(raw.get("planner"), _DEFAULT_PLANNER, _DEFAULT_PLANNER_FALLBACKS)
    worker = _route(raw.get("worker"), _DEFAULT_WORKER, ())
    reviewer = _route(raw.get("reviewer"), _DEFAULT_PLANNER, _DEFAULT_PLANNER_FALLBACKS)
    return ModelRoutes(planner=planner, worker=worker, reviewer=reviewer)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError("planner response has no choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping)
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("planner response has no text content")
    return content.strip()


def _parse_json(text: str) -> Dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.I | re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("planner did not return valid plan JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("planner response must be a JSON object")
    return payload


class ModelPlanner:
    """Call a strong plan-only model with an explicit fallback chain."""

    def __init__(self, routes: ModelRoutes, *, call_llm: Optional[Callable[..., Any]] = None):
        self.routes = routes
        self._call_llm = call_llm

    def _call(self, target: ModelTarget, messages: List[Dict[str, str]]) -> Any:
        call = self._call_llm
        if call is None:
            from agent.auxiliary_client import call_llm

            call = call_llm
        return call(
            task="orchestration_planner",
            messages=messages,
            tools=[],
            max_tokens=4096,
            timeout=180,
            **target.as_call_kwargs(),
        )

    @staticmethod
    def _messages(objective: str, context: Optional[str]) -> List[Dict[str, str]]:
        system = (
            "You are Hermes' planning-only coordinator. You must not execute commands, "
            "call tools, modify files, browse, publish, send messages, or claim that "
            "anything has been completed. Return only one valid JSON object matching "
            "the orchestration Plan schema: plan_id, objective, execution_mode, "
            "total_time_budget_seconds, and tasks. For durable execution, "
            "total_time_budget_seconds is required: it must be a finite positive "
            "whole number of seconds, and it must never be omitted or null. "
            "Each task must include id, title, role, backend, and depends_on. "
            "Use role coordinator for a direct task, researcher for independent "
            "research, architect/coder/tester/reviewer for durable software work. "
            "Choose the smallest safe plan and include verification-oriented tasks."
        )
        user = "Objective:\n" + objective.strip()
        if context:
            user += "\n\nContext:\n" + context.strip()
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def plan(self, objective: str, *, context: Optional[str] = None) -> PlannerResult:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must not be empty")
        from agent.orchestrator import Orchestrator

        messages = self._messages(objective, context)
        attempted: List[ModelTarget] = []
        last_error: Optional[BaseException] = None
        for target in self.routes.planner.candidates():
            attempted.append(target)
            try:
                payload = _parse_json(_response_text(self._call(target, messages)))
                plan = Orchestrator().plan_from_mapping(payload)
                return PlannerResult(
                    plan=plan,
                    actual_provider=target.provider,
                    actual_model=target.model,
                    fallback_used=target != self.routes.planner.primary,
                    attempted=tuple(attempted),
                )
            except Exception as exc:
                last_error = exc
        raise ValueError("all planner models failed: %s" % last_error) from last_error


__all__ = [
    "ModelPlanner",
    "ModelRoutes",
    "ModelTarget",
    "PlannerResult",
    "RoleRoute",
    "resolve_model_routes",
]
