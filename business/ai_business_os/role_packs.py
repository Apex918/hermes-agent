from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .adapters import ReadOnlyAdapter
from .contracts import ActionRequest, DecisionRecord, EvidenceEnvelope


@dataclass(frozen=True)
class RolePack:
    """One business role pack with a read-only adapter chain and handoff."""

    role: str
    manifest: dict[str, Any]
    proposal: dict[str, Any]
    decision: dict[str, Any]
    adapters: tuple[ReadOnlyAdapter, ...]

    def _approval_required(self) -> bool:
        return bool(self.decision.get("approval_required"))

    def collect_evidence(
        self,
        snapshots: Mapping[str, Mapping[str, Any] | None],
        *,
        now: datetime | None = None,
    ) -> tuple[EvidenceEnvelope, ...]:
        return tuple(
            adapter.inspect(snapshots.get(adapter.adapter_id), now=now)
            for adapter in self.adapters
        )

    def build_handoff(
        self,
        evidence: tuple[EvidenceEnvelope, ...],
        *,
        blocked: bool,
    ) -> ActionRequest:
        gaps = tuple(item.adapter_id for item in evidence if item.blocking)
        approval_required = self._approval_required()
        if blocked:
            summary = f"{self.role} blocked; missing or expired data"
            next_cards = tuple(f"{self.role}: escalate {gap}" for gap in gaps)
        elif approval_required:
            summary = f"{self.role} read-only scan complete; pending approval for high-risk output"
            next_cards = (f"{self.role}: approval review",)
        else:
            summary = f"{self.role} read-only scan complete; Kanban handoff ready"
            next_cards = (f"{self.role}: kanban handoff",)
        return ActionRequest(
            role=self.role,
            summary=summary,
            evidence=evidence,
            gaps=gaps,
            next_cards=next_cards,
        )

    def evaluate(
        self,
        snapshots: Mapping[str, Mapping[str, Any] | None],
        *,
        now: datetime | None = None,
    ) -> DecisionRecord:
        evidence = self.collect_evidence(snapshots, now=now)
        gaps = tuple(item.adapter_id for item in evidence if item.blocking)
        blocked = bool(gaps)
        approval_required = self._approval_required()
        handoff = self.build_handoff(evidence, blocked=blocked).to_dict()
        if blocked:
            return DecisionRecord(
                role=self.role,
                verdict="blocked",
                reason=f"{self.role} missing or expired data; Kanban handoff blocked",
                evidence=evidence,
                handoff=handoff,
            )
        if approval_required:
            return DecisionRecord(
                role=self.role,
                verdict="pending_approval",
                reason=f"{self.role} evidence fresh; pending approval for high-risk output",
                evidence=evidence,
                handoff=handoff,
            )
        return DecisionRecord(
            role=self.role,
            verdict="ready",
            reason=f"{self.role} evidence fresh; Kanban handoff ready",
            evidence=evidence,
            handoff=handoff,
        )


def _pack(
    role: str,
    adapter_specs: list[tuple[str, str, int, tuple[str, ...]]],
    *,
    tier: str = "P0",
    priority: int = 0,
    approval_required: bool = False,
    sensitive_scope: tuple[str, ...] = (),
) -> RolePack:
    adapters = tuple(
        ReadOnlyAdapter(
            adapter_id=adapter_id,
            source=source,
            freshness_limit_hours=freshness_hours,
            required_fields=required_fields,
        )
        for adapter_id, source, freshness_hours, required_fields in adapter_specs
    )
    manifest = {
        "role": role,
        "tier": tier,
        "priority": priority,
        "risk_level": "high" if approval_required else "standard",
        "evidence_coverage": 100,
        "adapter_ids": [adapter.adapter_id for adapter in adapters],
    }
    if sensitive_scope:
        manifest["sensitive_scope"] = list(sensitive_scope)
    proposal = {
        "role": role,
        "read_only_adapters": [adapter.adapter_id for adapter in adapters],
        "kanban_handoff": "required",
        "evidence_freshness_hours": {
            adapter.adapter_id: adapter.freshness_limit_hours for adapter in adapters
        },
        "approval_required": approval_required,
    }
    if sensitive_scope:
        proposal["sensitive_scope"] = list(sensitive_scope)
    decision = {
        "role": role,
        "verdict": "pending_approval" if approval_required else "pending_evidence",
        "approval_required": approval_required,
        "approval_state": "pending_approval" if approval_required else "ready",
        "fail_closed_on": ["missing", "expired"],
        "missing_or_expired_action": "block_and_handoff",
    }
    if sensitive_scope:
        decision["sensitive_scope"] = list(sensitive_scope)
    return RolePack(
        role=role,
        manifest=manifest,
        proposal=proposal,
        decision=decision,
        adapters=adapters,
    )


def build_p0_role_packs() -> list[RolePack]:
    """Build the eight P0 baseline business role packs."""

    return [
        _pack(
            "CEO",
            [
                ("board", "board snapshot", 24, ("observed_at",)),
                ("calendar", "executive calendar", 24, ("observed_at",)),
                ("decision_log", "decision log", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CFO",
            [
                ("ledger", "ledger export", 24, ("observed_at",)),
                ("cash_runway", "cash runway report", 24, ("observed_at",)),
                ("invoice_queue", "invoice queue", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "COO",
            [
                ("delivery_board", "delivery board", 24, ("observed_at",)),
                ("project_tracker", "project tracker", 24, ("observed_at",)),
                ("incident_log", "incident log", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CMO",
            [
                ("campaign_calendar", "campaign calendar", 24, ("observed_at",)),
                ("analytics", "marketing analytics", 24, ("observed_at",)),
                ("content_pipeline", "content pipeline", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CRO",
            [
                ("crm_pipeline", "CRM pipeline", 24, ("observed_at",)),
                ("sales_forecast", "sales forecast", 24, ("observed_at",)),
                ("proposal_queue", "proposal queue", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CPO",
            [
                ("roadmap", "product roadmap", 24, ("observed_at",)),
                ("feedback_queue", "feedback queue", 24, ("observed_at",)),
                ("research_notes", "research notes", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CTO",
            [
                ("repo_status", "repository status", 24, ("observed_at",)),
                ("ci_pipeline", "CI pipeline", 24, ("observed_at",)),
                ("architecture_notes", "architecture notes", 24, ("observed_at",)),
            ],
        ),
        _pack(
            "CISO",
            [
                ("access_review", "access review log", 24, ("observed_at",)),
                ("security_queue", "security queue", 24, ("observed_at",)),
                ("vuln_backlog", "vulnerability backlog", 24, ("observed_at",)),
            ],
        ),
    ]


def build_p1_sensitive_role_packs() -> list[RolePack]:
    """Build the four P1 sensitive business role packs."""

    return [
        _pack(
            "CHRO",
            [
                ("people_roster", "people roster", 12, ("observed_at",)),
                ("hiring_queue", "hiring queue", 12, ("observed_at",)),
                ("benefits_queue", "benefits queue", 12, ("observed_at",)),
            ],
            tier="P1",
            priority=1,
            approval_required=True,
            sensitive_scope=("PII", "people-ops", "employment"),
        ),
        _pack(
            "GC",
            [
                ("legal_matter_log", "legal matter log", 12, ("observed_at",)),
                ("contract_queue", "contract queue", 12, ("observed_at",)),
                ("policy_register", "policy register", 12, ("observed_at",)),
            ],
            tier="P1",
            priority=1,
            approval_required=True,
            sensitive_scope=("legal", "rights", "contracts"),
        ),
        _pack(
            "CCO",
            [
                ("customer_outreach_queue", "customer outreach queue", 12, ("observed_at",)),
                ("customer_comms_calendar", "customer comms calendar", 12, ("observed_at",)),
                ("escalation_log", "escalation log", 12, ("observed_at",)),
            ],
            tier="P1",
            priority=1,
            approval_required=True,
            sensitive_scope=("customer-outreach", "communications", "reputation"),
        ),
        _pack(
            "CDO",
            [
                ("data_rights_requests", "data rights requests", 12, ("observed_at",)),
                ("pii_inventory", "PII inventory", 12, ("observed_at",)),
                ("consent_register", "consent register", 12, ("observed_at",)),
            ],
            tier="P1",
            priority=1,
            approval_required=True,
            sensitive_scope=("PII", "rights", "consent"),
        ),
    ]


def build_sensitive_role_packs() -> list[RolePack]:
    """Alias for the P1 sensitive role packs."""

    return build_p1_sensitive_role_packs()


def build_all_role_packs() -> list[RolePack]:
    """Build the full 12-pack baseline plus sensitive role set."""

    return build_p0_role_packs() + build_p1_sensitive_role_packs()
