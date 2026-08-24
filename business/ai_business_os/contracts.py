from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class EvidenceEnvelope:
    """One read-only evidence observation from a business adapter."""

    adapter_id: str
    source: str
    observed_at: datetime | None
    freshness: str
    notes: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.freshness in {"missing", "expired"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness": self.freshness,
            "notes": self.notes,
            "payload": dict(self.payload),
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ActionRequest:
    """Kanban handoff proposal emitted by a role pack."""

    role: str
    summary: str
    evidence: tuple[EvidenceEnvelope, ...] = ()
    gaps: tuple[str, ...] = ()
    next_cards: tuple[str, ...] = ()
    kanban_handoff: str = "required"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "summary": self.summary,
            "evidence": [item.to_dict() for item in self.evidence],
            "gaps": list(self.gaps),
            "next_cards": list(self.next_cards),
            "kanban_handoff": self.kanban_handoff,
        }


@dataclass(frozen=True)
class DecisionRecord:
    """Final evaluation of a role pack's evidence state."""

    role: str
    verdict: str
    reason: str
    evidence: tuple[EvidenceEnvelope, ...] = ()
    handoff: dict[str, Any] = field(default_factory=dict)
    fail_closed_on: tuple[str, ...] = ("missing", "expired")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
            "handoff": dict(self.handoff),
            "fail_closed_on": list(self.fail_closed_on),
        }
