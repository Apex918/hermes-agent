"""Offline, read-only coordination boundary for three-system context.

The adapter accepts a local command-center artifact or an explicit fixture that
contains Mission/Evidence/Decision/ActionRequest records.  It validates the
context, asks Hermes' existing :class:`agent.orchestrator.Orchestrator` to
build a plan, and returns a serialisable coordination result.  It deliberately
does not run delegates, open a network connection, touch Kanban, or perform a
write/trading operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol

from agent.orchestrator import Orchestrator


INTEGRATION_STATUS = "coordination-only/worktree-only"
_ALLOWED_SOURCE_KINDS = {"explicit-fixture", "local-command-center-artifact"}
_DANGEROUS_ACTION_RE = re.compile(
    r"(?<![a-z0-9])(?:submit|place|cancel|amend|execute|withdraw|transfer|write|delete|update|"
    r"network|websocket|redis|ruoyi|mario|production|live|trading|order|trade|"
    r"position|balance|wallet|payment|database|db|auth|credential|api)(?![a-z0-9])",
    re.IGNORECASE,
)


class CoordinationAdapterError(ValueError):
    """Raised when a coordination context is malformed or not local."""


class OfflinePolicyError(CoordinationAdapterError):
    """Raised when a request attempts to leave the read-only offline boundary."""


class _Planner(Protocol):
    def plan(self, objective: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CoordinationPolicy:
    """Immutable fail-closed policy for this adapter version."""

    network_access: bool = False
    live_trading: bool = False
    order_submission: str = "forbidden"
    external_effects: int = 0
    integration_status: str = INTEGRATION_STATUS

    def __post_init__(self) -> None:
        if self.network_access is not False:
            raise OfflinePolicyError("network_access must be false")
        if self.live_trading is not False:
            raise OfflinePolicyError("live_trading must be false")
        if self.order_submission != "forbidden":
            raise OfflinePolicyError("order_submission must be forbidden")
        if type(self.external_effects) is not int or self.external_effects != 0:
            raise OfflinePolicyError("external_effects must be zero")
        if self.integration_status != INTEGRATION_STATUS:
            raise OfflinePolicyError(
                f"integration_status must be {INTEGRATION_STATUS!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_access": self.network_access,
            "live_trading": self.live_trading,
            "order_submission": self.order_submission,
            "external_effects": self.external_effects,
            "integration_status": self.integration_status,
        }


@dataclass(frozen=True)
class Mission:
    """Minimal validated mission envelope used for planning."""

    mission_id: str
    objective: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Mission":
        if not isinstance(value, Mapping):
            raise CoordinationAdapterError("mission must be an object")
        mission_id = value.get("mission_id", value.get("id"))
        objective = value.get("objective", value.get("goal"))
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise CoordinationAdapterError("mission_id is required")
        if not isinstance(objective, str) or not objective.strip():
            raise CoordinationAdapterError("mission objective is required")
        return cls(mission_id.strip(), objective.strip())

    def to_dict(self) -> dict[str, str]:
        return {"mission_id": self.mission_id, "objective": self.objective}


@dataclass(frozen=True)
class CoordinationContext:
    """Validated structured context passed to the orchestration boundary."""

    mission: Mission
    evidence: tuple[Mapping[str, Any], ...]
    decision: Mapping[str, Any]
    action_request: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoordinationContext":
        if not isinstance(value, Mapping):
            raise CoordinationAdapterError("coordination context must be an object")
        mission = Mission.from_dict(value.get("mission"))
        evidence = value.get("evidence")
        if not isinstance(evidence, (list, tuple)):
            raise CoordinationAdapterError("evidence must be a list")
        evidence_items: list[Mapping[str, Any]] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                raise CoordinationAdapterError("each evidence item must be an object")
            evidence_items.append(dict(item))
        decision = value.get("decision")
        if not isinstance(decision, Mapping):
            raise CoordinationAdapterError("decision must be an object")
        action_request = value.get("action_request")
        if not isinstance(action_request, Mapping):
            raise CoordinationAdapterError("action_request must be an object")
        request_id = action_request.get("request_id", action_request.get("id"))
        if not isinstance(request_id, str) or not request_id.strip():
            raise CoordinationAdapterError("action_request request_id is required")
        action = action_request.get("action", action_request.get("summary"))
        if not isinstance(action, str) or not action.strip():
            raise CoordinationAdapterError("action_request action is required")
        _reject_unsafe_action(action_request)
        return cls(mission, tuple(evidence_items), dict(decision), dict(action_request))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission": self.mission.to_dict(),
            "evidence": [dict(item) for item in self.evidence],
            "decision": dict(self.decision),
            "action_request": dict(self.action_request),
        }


@dataclass(frozen=True)
class CoordinationResult:
    """Read-only plan result with explicit policy and source provenance."""

    status: str
    context: dict[str, Any]
    plan: dict[str, Any]
    policy: CoordinationPolicy
    provenance: dict[str, Any]
    external_effects: int = 0
    integration_status: str = INTEGRATION_STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "context": self.context,
            "plan": self.plan,
            "policy": self.policy.to_dict(),
            "provenance": dict(self.provenance),
            "external_effects": self.external_effects,
            "integration_status": self.integration_status,
        }


class HermesCoordinationAdapter:
    """Build an offline coordination plan through Hermes' existing planner."""

    def __init__(
        self,
        *,
        orchestrator: _Planner | None = None,
        policy: CoordinationPolicy | None = None,
    ) -> None:
        self.orchestrator = orchestrator or Orchestrator()
        self.policy = policy or CoordinationPolicy()
        if self.policy != CoordinationPolicy():
            raise OfflinePolicyError("only the default offline policy is supported")

    def coordinate(
        self,
        source: Mapping[str, Any] | str | Path | bytes,
        *,
        source_kind: str | None = None,
    ) -> CoordinationResult:
        context_payload, provenance = self._load(source, source_kind=source_kind)
        context = CoordinationContext.from_dict(context_payload)
        try:
            plan = self.orchestrator.plan(
                context.mission.objective,
                plan_id=f"coordination-{context.mission.mission_id}",
            )
        except CoordinationAdapterError:
            raise
        except Exception as exc:
            raise CoordinationAdapterError("Hermes orchestration planning failed") from exc
        plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else plan
        if not isinstance(plan_dict, Mapping):
            raise CoordinationAdapterError("Hermes planner returned a non-object plan")
        return CoordinationResult(
            status="coordination-only",
            context=context.to_dict(),
            plan=dict(plan_dict),
            policy=self.policy,
            provenance=provenance,
        )

    run = coordinate

    def coordinate_fixture(self, fixture: Mapping[str, Any] | bytes | str) -> CoordinationResult:
        if isinstance(fixture, str):
            try:
                fixture = json.loads(fixture)
            except json.JSONDecodeError as exc:
                raise CoordinationAdapterError("fixture text must contain JSON") from exc
        return self.coordinate(fixture, source_kind="explicit-fixture")

    def coordinate_artifact(self, path: str | Path) -> CoordinationResult:
        return self.coordinate(path, source_kind="local-command-center-artifact")

    @staticmethod
    def _load(
        source: Mapping[str, Any] | str | Path | bytes,
        *,
        source_kind: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if source_kind is not None and source_kind not in _ALLOWED_SOURCE_KINDS:
            raise CoordinationAdapterError("only explicit fixtures and local artifacts are supported")
        if isinstance(source, Mapping):
            payload = dict(source)
            kind = source_kind or str(payload.get("source_kind") or "explicit-fixture")
            provenance: dict[str, Any] = {"source_kind": kind}
        elif isinstance(source, bytes):
            kind = source_kind or "explicit-fixture"
            try:
                payload = json.loads(source.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CoordinationAdapterError("fixture bytes must contain JSON") from exc
            provenance = {"source_kind": kind}
        else:
            text = str(source)
            if "://" in text:
                raise CoordinationAdapterError("network artifact sources are not permitted")
            path = Path(source).expanduser()
            kind = source_kind or "local-command-center-artifact"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinationAdapterError("cannot read local command-center artifact") from exc
            provenance = {"source_kind": kind, "path": str(path)}
        if kind not in _ALLOWED_SOURCE_KINDS:
            raise CoordinationAdapterError("only explicit fixtures and local artifacts are supported")
        if not isinstance(payload, dict):
            raise CoordinationAdapterError("coordination source root must be an object")
        # A command-center export may wrap the structured context once.  Do not
        # follow arbitrary nested values or references that could become I/O.
        nested = payload.get("context")
        if isinstance(nested, Mapping):
            payload = dict(nested)
        required = ("mission", "evidence", "decision", "action_request")
        missing = [key for key in required if key not in payload]
        if missing:
            raise CoordinationAdapterError("context missing " + ", ".join(missing))
        return payload, provenance


def _reject_unsafe_action(action_request: Mapping[str, Any]) -> None:
    """Reject effectful requests without interpreting them as permissions."""

    def values(value: Any):
        if isinstance(value, Mapping):
            for key, item in value.items():
                yield str(key)
                yield from values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from values(item)
        elif value is not None:
            yield str(value)

    for value in values(action_request):
        if "://" in value:
            raise OfflinePolicyError(
                "action_request contains an external URL"
            )
        if _DANGEROUS_ACTION_RE.search(value):
            raise OfflinePolicyError(
                "action_request requests a live, write, order, or external operation"
            )


CoordinationAdapter = HermesCoordinationAdapter

__all__ = [
    "CoordinationAdapter",
    "CoordinationAdapterError",
    "CoordinationContext",
    "CoordinationPolicy",
    "CoordinationResult",
    "HermesCoordinationAdapter",
    "INTEGRATION_STATUS",
    "Mission",
    "OfflinePolicyError",
]
