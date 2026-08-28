"""Versioned, serialisable contracts for the AI Business OS.

The adapter-facing records at the top of this module remain backwards
compatible with the original read-only role-pack API.  Canonical records are
created with ``from_dict`` and are validated against the local JSON Schemas;
no constructor or reader performs network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Mapping


class ContractValidationError(ValueError):
    """Raised when a versioned contract cannot be safely consumed."""

    def __init__(self, contract: str, errors: list[str]) -> None:
        self.contract = contract
        self.errors = tuple(errors)
        super().__init__(f"invalid {contract} contract: " + "; ".join(errors))


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _read_artifact(artifact: Mapping[str, Any] | str | Path | bytes) -> dict[str, Any]:
    """Read a local parent artifact, deliberately rejecting URL-like inputs."""

    if isinstance(artifact, Mapping):
        return dict(artifact)
    if isinstance(artifact, bytes):
        value = json.loads(artifact.decode("utf-8"))
    else:
        if isinstance(artifact, str) and "://" in artifact:
            raise ContractValidationError("parent-artifact", ["network artifact sources are not permitted"])
        path = Path(artifact)
        value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractValidationError("parent-artifact", ["artifact root must be an object"])
    return value


def _from_artifact(artifact: Mapping[str, Any] | str | Path | bytes, key: str) -> dict[str, Any]:
    payload = _read_artifact(artifact)
    if "schema_version" in payload:
        return payload
    # Parent artifacts commonly wrap records under artifact, payload, or the
    # contract name.  Only local structured data is followed.
    for wrapper in ("artifact", "payload", "data", "contract"):
        nested = payload.get(wrapper)
        if isinstance(nested, Mapping):
            if "schema_version" in nested:
                return dict(nested)
            for candidate_key in (key, key.replace("_", "-")):
                candidate = nested.get(candidate_key)
                if isinstance(candidate, Mapping):
                    return dict(candidate)
    for candidate_key in (key, key.replace("_", "-")):
        candidate = payload.get(candidate_key)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    raise ContractValidationError("parent-artifact", [f"missing {key!r} contract"])


def _validate(contract: str, payload: Mapping[str, Any]) -> None:
    # Lazy import avoids a validator -> contracts import cycle.
    from .validator import validate_named_contract

    errors = validate_named_contract(contract, dict(payload))
    if errors:
        raise ContractValidationError(contract, errors)


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Source-grounded evidence with provenance and use restrictions."""

    # Legacy adapter fields are retained for existing role-pack callers.
    adapter_id: str = ""
    source: str = ""
    observed_at: datetime | None = None
    freshness: Any = ""
    notes: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    # Canonical EvidenceEnvelope.v1 fields.
    source_id: str = ""
    as_of: Any = None
    license_or_entitlement: str = ""
    pii_class: str = ""
    confidence: float | None = None
    allowed_use: Any = ()
    hash: str = ""
    evidence_id: str = ""
    schema_version: str | None = None
    manifest_version: int | None = None
    subject: str = ""
    evidence: tuple[Mapping[str, Any], ...] = ()
    rights: Mapping[str, Any] | None = None
    license: str = ""

    @property
    def blocking(self) -> bool:
        if isinstance(self.freshness, Mapping):
            return str(self.freshness.get("status", "")).lower() in {"missing", "expired", "stale", "unknown"}
        return self.freshness in {"missing", "expired", "stale", "unknown"}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceEnvelope":
        data = dict(payload)
        _validate("evidence-envelope", data)
        return cls(
            adapter_id=str(data.get("adapter_id", "")),
            source=data["source"],
            observed_at=_coerce_datetime(data.get("observed_at")),
            freshness=data["freshness"],
            notes=str(data.get("notes", "")),
            payload=data.get("payload", {}),
            source_id=data["source_id"],
            as_of=data["as_of"],
            license_or_entitlement=data["license_or_entitlement"],
            pii_class=data["pii_class"],
            confidence=data["confidence"],
            allowed_use=data["allowed_use"],
            hash=data["hash"],
            evidence_id=data["evidence_id"],
            schema_version=data["schema_version"],
            manifest_version=data["manifest_version"],
            subject=str(data.get("subject", "")),
            evidence=tuple(data.get("evidence", ())),
            rights=data.get("rights"),
            license=str(data.get("license", "")),
        )

    @classmethod
    def from_parent_artifact(cls, artifact: Mapping[str, Any] | str | Path | bytes) -> "EvidenceEnvelope":
        return cls.from_dict(_from_artifact(artifact, "evidence_envelope"))

    read_from_artifact = from_parent_artifact

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version or self.source_id or self.evidence_id:
            result: dict[str, Any] = {
                "schema_version": self.schema_version or "hermes.ai_business_os.evidence_envelope.v1",
                "manifest_version": self.manifest_version if self.manifest_version is not None else 1,
                "evidence_id": self.evidence_id,
                "source": self.source,
                "source_id": self.source_id,
                "as_of": _json_value(self.as_of),
                "freshness": _json_value(self.freshness),
                "license_or_entitlement": self.license_or_entitlement,
                "pii_class": self.pii_class,
                "confidence": self.confidence,
                "allowed_use": _json_value(self.allowed_use),
                "hash": self.hash,
            }
            for key, value in (("subject", self.subject), ("evidence", self.evidence), ("rights", self.rights), ("license", self.license)):
                if value not in ("", (), None):
                    result[key] = _json_value(value)
            return result
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
class DecisionRecord:
    """An owned, reviewable decision linked to evidence."""

    role: str = ""
    verdict: str = ""
    reason: str = ""
    evidence: tuple[EvidenceEnvelope, ...] = ()
    handoff: dict[str, Any] = field(default_factory=dict)
    fail_closed_on: tuple[str, ...] = ("missing", "expired")
    owner: str = ""
    approver: str | None = None
    review_date: Any = None
    decision_id: str = ""
    decision: str = ""
    rationale: str = ""
    evidence_refs: tuple[str, ...] = ()
    audit_ref: str = ""
    schema_version: str | None = None
    manifest_version: int | None = None
    subject: str = ""
    rights: Mapping[str, Any] | None = None
    freshness: Any = None
    license: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DecisionRecord":
        data = dict(payload)
        _validate("decision-record", data)
        return cls(
            role=str(data.get("role", "")), verdict=str(data.get("verdict", "")), reason=str(data.get("reason", "")),
            evidence_refs=tuple(data.get("evidence_refs", ())), owner=data["owner"], approver=data.get("approver"),
            review_date=data["review_date"], decision_id=data["decision_id"], decision=data["decision"],
            rationale=data.get("rationale", ""), audit_ref=str(data.get("audit_ref", "")),
            schema_version=data["schema_version"],
            manifest_version=data["manifest_version"], subject=str(data.get("subject", "")),
            rights=data.get("rights"), freshness=data.get("freshness"), license=str(data.get("license", "")),
        )

    @classmethod
    def from_parent_artifact(cls, artifact: Mapping[str, Any] | str | Path | bytes) -> "DecisionRecord":
        return cls.from_dict(_from_artifact(artifact, "decision_record"))

    read_from_artifact = from_parent_artifact

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version:
            result: dict[str, Any] = {
                "schema_version": self.schema_version, "manifest_version": self.manifest_version,
                "decision_id": self.decision_id, "owner": self.owner, "approver": self.approver,
                "review_date": _json_value(self.review_date), "decision": self.decision,
            }
            for key, value in (("subject", self.subject), ("rationale", self.rationale), ("evidence_refs", self.evidence_refs), ("audit_ref", self.audit_ref), ("rights", self.rights), ("freshness", self.freshness), ("license", self.license)):
                if value not in ("", (), None):
                    result[key] = _json_value(value)
            return result
        return {
            "role": self.role, "verdict": self.verdict, "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence], "handoff": dict(self.handoff),
            "fail_closed_on": list(self.fail_closed_on),
        }


@dataclass(frozen=True)
class ActionRequest:
    """A dry-run action with explicit effect, approval, rollback and audit data."""

    role: str = ""
    summary: str = ""
    evidence: tuple[EvidenceEnvelope, ...] = ()
    gaps: tuple[str, ...] = ()
    next_cards: tuple[str, ...] = ()
    kanban_handoff: str = "required"
    owner: str = ""
    effect_level: str = ""
    preview: Any = None
    rollback: Any = None
    approvers: tuple[str, ...] = ()
    idempotency_key: str = ""
    audit_ref: str = ""
    request_id: str = ""
    action: str = ""
    evidence_refs: tuple[str, ...] = ()
    schema_version: str | None = None
    manifest_version: int | None = None
    subject: str = ""
    constraints: tuple[str, ...] = ()
    rights: Mapping[str, Any] | None = None
    freshness: Any = None
    license: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionRequest":
        data = dict(payload)
        _validate("action-request", data)
        return cls(
            role=str(data.get("role", "")), summary=str(data.get("summary", "")),
            owner=data["owner"], effect_level=data["effect_level"], preview=data["preview"],
            rollback=data["rollback"], approvers=tuple(data["approvers"]), idempotency_key=data["idempotency_key"],
            audit_ref=data["audit_ref"], request_id=data["request_id"], action=data["action"],
            evidence_refs=tuple(data.get("evidence_refs", ())), schema_version=data["schema_version"],
            manifest_version=data["manifest_version"], subject=str(data.get("subject", "")),
            constraints=tuple(data.get("constraints", ())), rights=data.get("rights"),
            freshness=data.get("freshness"), license=str(data.get("license", "")),
        )

    @classmethod
    def from_parent_artifact(cls, artifact: Mapping[str, Any] | str | Path | bytes) -> "ActionRequest":
        return cls.from_dict(_from_artifact(artifact, "action_request"))

    read_from_artifact = from_parent_artifact

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version:
            result: dict[str, Any] = {
                "schema_version": self.schema_version, "manifest_version": self.manifest_version,
                "request_id": self.request_id, "owner": self.owner, "effect_level": self.effect_level,
                "preview": _json_value(self.preview), "rollback": _json_value(self.rollback),
                "approvers": list(self.approvers), "idempotency_key": self.idempotency_key,
                "audit_ref": self.audit_ref, "action": self.action,
            }
            for key, value in (("subject", self.subject), ("evidence_refs", self.evidence_refs), ("constraints", self.constraints), ("rights", self.rights), ("freshness", self.freshness), ("license", self.license)):
                if value not in ("", (), None):
                    result[key] = _json_value(value)
            return result
        return {
            "role": self.role, "summary": self.summary,
            "evidence": [item.to_dict() for item in self.evidence], "gaps": list(self.gaps),
            "next_cards": list(self.next_cards), "kanban_handoff": self.kanban_handoff,
        }


@dataclass(frozen=True)
class WorkItem:
    """Versioned unit of work that can be reconstructed from a parent artifact."""

    work_item_id: str
    title: str
    owner: str
    effect_level: str
    acceptance_criteria: tuple[str, ...]
    idempotency_key: str
    audit_ref: str = ""
    status: str = "pending"
    priority: str = "P1"
    evidence_refs: tuple[str, ...] = ()
    parent_artifact_ref: str | None = None
    requested_action: str = ""
    schema_version: str = "hermes.ai_business_os.work_item.v1"
    manifest_version: int = 1

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkItem":
        data = dict(payload)
        _validate("work-item", data)
        return cls(
            work_item_id=data["work_item_id"], title=data["title"], owner=data["owner"],
            effect_level=data["effect_level"], acceptance_criteria=tuple(data["acceptance_criteria"]),
            idempotency_key=data["idempotency_key"], audit_ref=data.get("audit_ref", ""),
            status=data.get("status", "pending"), priority=data.get("priority", "P1"),
            evidence_refs=tuple(data.get("evidence_refs", ())), parent_artifact_ref=data.get("parent_artifact_ref"),
            requested_action=data.get("requested_action", ""), schema_version=data["schema_version"],
            manifest_version=data["manifest_version"],
        )

    @classmethod
    def from_parent_artifact(cls, artifact: Mapping[str, Any] | str | Path | bytes) -> "WorkItem":
        return cls.from_dict(_from_artifact(artifact, "work_item"))

    read_from_artifact = from_parent_artifact

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version, "manifest_version": self.manifest_version,
            "work_item_id": self.work_item_id, "title": self.title, "owner": self.owner,
            "effect_level": self.effect_level, "acceptance_criteria": list(self.acceptance_criteria),
            "idempotency_key": self.idempotency_key, "audit_ref": self.audit_ref, "status": self.status,
            "priority": self.priority, "evidence_refs": list(self.evidence_refs),
        }
        if self.parent_artifact_ref is not None:
            result["parent_artifact_ref"] = self.parent_artifact_ref
        if self.requested_action:
            result["requested_action"] = self.requested_action
        return result


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "ActionRequest", "ContractValidationError", "DecisionRecord", "EvidenceEnvelope", "WorkItem",
]
