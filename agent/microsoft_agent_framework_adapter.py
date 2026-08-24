"""High-level Microsoft Agent Framework proof adapter.

This is a fake, offline proof that Hermes can keep routing, profile,
approval, and audit decisions under Hermes control while projecting a
human-in-the-loop subgraph into a Microsoft Agent Framework-shaped graph.
No external provider calls are made; the bundled provider is deliberately
fake and network-off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from uuid import uuid4

from agent.orchestrator import Orchestrator, Plan, PlanValidationError


@dataclass
class FakeMicrosoftAgentFrameworkProvider:
    """Deterministic offline provider used by the proof adapter."""

    name: str = "fake-microsoft-agent-framework"
    network_off: bool = True
    calls: int = 0
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self, plan: Plan) -> dict[str, Any]:
        self.calls += 1
        payload = {
            "name": self.name,
            "provider": self.name,
            "network_off": self.network_off,
            "calls": self.calls,
            "plan_id": plan.plan_id,
            "objective": plan.objective,
            "task_count": len(plan.tasks),
        }
        self.snapshots.append(payload)
        return payload


@dataclass(frozen=True)
class MicrosoftAgentFrameworkProof:
    proof_id: str
    framework: str
    plan: dict[str, Any]
    hitl_subgraph: dict[str, Any]
    hermes_ownership: dict[str, Any]
    provider: dict[str, Any]
    audit_trail: tuple[dict[str, Any], ...] = ()
    network_off: bool = True
    completed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "framework": self.framework,
            "plan": self.plan,
            "hitl_subgraph": self.hitl_subgraph,
            "hermes_ownership": self.hermes_ownership,
            "provider": self.provider,
            "audit_trail": [dict(item) for item in self.audit_trail],
            "network_off": self.network_off,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MicrosoftAgentFrameworkProof":
        return cls(
            proof_id=str(payload.get("proof_id", "")),
            framework=str(payload.get("framework", "microsoft-agent-framework")),
            plan=dict(payload.get("plan") or {}),
            hitl_subgraph=dict(payload.get("hitl_subgraph") or {}),
            hermes_ownership=dict(payload.get("hermes_ownership") or {}),
            provider=dict(payload.get("provider") or {}),
            audit_trail=tuple(
                dict(item)
                for item in payload.get("audit_trail", [])
                if isinstance(item, Mapping)
            ),
            network_off=bool(payload.get("network_off", True)),
            completed=bool(payload.get("completed", True)),
        )


class MicrosoftAgentFrameworkAdapter:
    """Offline proof adapter for a Microsoft Agent Framework-shaped graph."""

    def __init__(
        self,
        *,
        orchestrator: Optional[Orchestrator] = None,
        provider: Optional[FakeMicrosoftAgentFrameworkProvider] = None,
    ):
        self.orchestrator = orchestrator or Orchestrator()
        self.provider = provider or FakeMicrosoftAgentFrameworkProvider()
        self._proofs: dict[str, MicrosoftAgentFrameworkProof] = {}

    def build_proof(self, plan_or_objective: Plan | Mapping[str, Any] | str) -> MicrosoftAgentFrameworkProof:
        plan = self._coerce_plan(plan_or_objective)
        provider_snapshot = self.provider.snapshot(plan)
        hitl_subgraph = self._build_hitl_subgraph(plan, provider_snapshot)
        proof = MicrosoftAgentFrameworkProof(
            proof_id="maf_" + uuid4().hex[:12],
            framework="microsoft-agent-framework",
            plan=plan.to_dict(),
            hitl_subgraph=hitl_subgraph,
            hermes_ownership=self._build_hermes_ownership(plan),
            provider=provider_snapshot,
            audit_trail=(
                {
                    "event": "provider_snapshot",
                    "payload": dict(provider_snapshot),
                },
                {
                    "event": "hitl_subgraph_built",
                    "payload": {
                        "node_count": len(hitl_subgraph["nodes"]),
                        "edge_count": len(hitl_subgraph["edges"]),
                    },
                },
            ),
        )
        self._proofs[proof.proof_id] = proof
        return proof

    def load_proof(self, proof: MicrosoftAgentFrameworkProof | Mapping[str, Any] | str) -> MicrosoftAgentFrameworkProof:
        if isinstance(proof, MicrosoftAgentFrameworkProof):
            self._proofs[proof.proof_id] = proof
            return proof
        if isinstance(proof, str):
            cached = self._proofs.get(proof)
            if cached is None:
                raise PlanValidationError(f"unknown Microsoft Agent Framework proof: {proof}")
            return cached
        loaded = MicrosoftAgentFrameworkProof.from_dict(proof)
        if not loaded.proof_id:
            raise PlanValidationError("Microsoft Agent Framework proof is missing proof_id")
        self._proofs[loaded.proof_id] = loaded
        return loaded

    def _coerce_plan(self, plan_or_objective: Plan | Mapping[str, Any] | str) -> Plan:
        if isinstance(plan_or_objective, Plan):
            return plan_or_objective
        if isinstance(plan_or_objective, str):
            return self.orchestrator.plan(plan_or_objective)
        return self.orchestrator.plan_from_mapping(plan_or_objective)

    def _build_hermes_ownership(self, plan: Plan) -> dict[str, Any]:
        routing = {task.id: "Hermes" for task in plan.tasks}
        profile = {
            task.id: (task.route.profile if task.route and task.route.profile else "default")
            for task in plan.tasks
        }
        approval = {
            "owner": "Hermes",
            "gate": "human_in_the_loop",
            "required": bool(plan.classification.needs_review or plan.execution_mode.value == "durable"),
        }
        audit = {
            "owner": "Hermes",
            "mode": "local_only",
            "scope": "offline-proof",
            "task_ids": [task.id for task in plan.tasks],
        }
        return {
            "routing": routing,
            "profile": profile,
            "approval": approval,
            "audit": audit,
        }

    def _build_hitl_subgraph(self, plan: Plan, provider_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        nodes = [
            {
                "id": "hermes.routing",
                "type": "routing",
                "owner": "Hermes",
                "mode": plan.execution_mode.value,
            },
            {
                "id": "agent_framework.graph",
                "type": "framework_graph",
                "owner": provider_snapshot.get("provider", self.provider.name),
                "plan_id": plan.plan_id,
                "task_count": len(plan.tasks),
            },
            {
                "id": "hermes.approval",
                "type": "human_in_the_loop",
                "owner": "Hermes",
                "gate": "approval",
            },
            {
                "id": "hermes.audit",
                "type": "audit",
                "owner": "Hermes",
                "gate": "local_only",
            },
        ]
        for task in plan.tasks:
            nodes.append(
                {
                    "id": f"task.{task.id}",
                    "type": "task",
                    "owner": task.route.profile if task.route and task.route.profile else task.role,
                    "role": task.role,
                    "backend": task.backend,
                    "depends_on": list(task.depends_on),
                }
            )

        nodes_by_id = {node["id"]: node for node in nodes}
        edges = [
            ("hermes.routing", "agent_framework.graph"),
            ("agent_framework.graph", "hermes.approval"),
            ("hermes.approval", "hermes.audit"),
        ]
        for task in plan.tasks:
            for dependency in task.depends_on:
                edges.append((f"task.{dependency}", f"task.{task.id}"))
            edges.append(("agent_framework.graph", f"task.{task.id}"))
            edges.append((f"task.{task.id}", "hermes.approval"))
        return {
            "framework": "microsoft-agent-framework",
            "provider": dict(provider_snapshot),
            "nodes": nodes,
            "nodes_by_id": nodes_by_id,
            "edges": edges,
            "hitl": {
                "entry": "hermes.routing",
                "gate": "hermes.approval",
                "audit": "hermes.audit",
            },
        }


__all__ = [
    "FakeMicrosoftAgentFrameworkProvider",
    "MicrosoftAgentFrameworkAdapter",
    "MicrosoftAgentFrameworkProof",
]
