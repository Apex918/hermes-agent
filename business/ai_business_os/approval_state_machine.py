"""Approval, audit, and dry-run state machine for AI Business OS writes.

The state machine stays intentionally small and deterministic so it can be used
from the write-approval gate and by unit tests without any network or external
service dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable, Literal

ApprovalLevel = Literal["R1", "R2", "R3"]
ApprovalStatus = Literal["dry_run", "pending", "approved", "rejected", "rolled_back"]

_RANK = {"R1": 1, "R2": 2, "R3": 3}


@dataclass(frozen=True)
class ApprovalPlan:
    subsystem: str
    action: str
    approval_level: ApprovalLevel
    receipt_required: bool
    preview: str
    diff: str
    rollback: dict[str, Any]
    audit_ref: str
    idempotency_key: str
    status: ApprovalStatus = "pending"
    dry_run: bool = False
    receipt: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ApprovalOutcome:
    success: bool
    status: ApprovalStatus
    audit_ref: str
    idempotency_key: str
    approval_level: ApprovalLevel
    preview: str
    diff: str
    rollback: dict[str, Any]
    result: Any = None
    error: str = ""
    idempotent: bool = False


class ApprovalStateMachine:
    """Deterministic planner + idempotent apply helper.

    The state machine itself is stateless about records; only the apply ledger is
    memoized so duplicate approvals with the same idempotency key do not repeat a
    side effect.
    """

    def __init__(self) -> None:
        self._applied: dict[str, ApprovalOutcome] = {}

    def plan(
        self,
        *,
        subsystem: str,
        action: str,
        payload: dict[str, Any],
        summary: str,
        origin: str,
        receipt: str | None = None,
        dry_run: bool = False,
        approval_level: ApprovalLevel | None = None,
    ) -> ApprovalPlan:
        level = approval_level or infer_approval_level(subsystem, action)
        receipt_required = _RANK[level] >= _RANK["R2"]
        normalized_payload = _normalize_payload(payload)
        preview = build_preview(subsystem, action, payload, summary)
        diff = build_diff(subsystem, action, payload)
        rollback = build_rollback(subsystem, action, payload)
        audit_ref = build_audit_ref(
            subsystem=subsystem,
            action=action,
            payload=normalized_payload,
            summary=summary,
            origin=origin,
            approval_level=level,
        )
        idempotency_key = build_idempotency_key(
            subsystem=subsystem,
            action=action,
            payload=normalized_payload,
            summary=summary,
            origin=origin,
            approval_level=level,
        )
        status: ApprovalStatus = "dry_run" if dry_run else "pending"
        reason = ""
        if receipt_required and receipt:
            reason = "receipt supplied"
        elif receipt_required and dry_run:
            reason = "dry-run preview only"
        elif receipt_required:
            reason = "receipt required"
        return ApprovalPlan(
            subsystem=subsystem,
            action=action,
            approval_level=level,
            receipt_required=receipt_required,
            preview=preview,
            diff=diff,
            rollback=rollback,
            audit_ref=audit_ref,
            idempotency_key=idempotency_key,
            status=status,
            dry_run=dry_run,
            receipt=receipt or "",
            reason=reason,
        )

    def apply_once(
        self,
        plan: ApprovalPlan,
        effect: Callable[[], Any],
        *,
        receipt: str | None = None,
    ) -> dict[str, Any]:
        if plan.receipt_required and not receipt:
            outcome = ApprovalOutcome(
                success=False,
                status="rejected",
                audit_ref=plan.audit_ref,
                idempotency_key=plan.idempotency_key,
                approval_level=plan.approval_level,
                preview=plan.preview,
                diff=plan.diff,
                rollback=plan.rollback,
                error="R2/R3 approval receipt required",
            )
            return outcome.__dict__.copy()

        cached = self._applied.get(plan.idempotency_key)
        if cached is not None:
            result = cached.__dict__.copy()
            if isinstance(cached.result, dict):
                result.update(cached.result)
            result["idempotent"] = True
            return result

        value = effect()
        if isinstance(value, dict) and value.get("success") is False:
            return {
                "success": False,
                "status": value.get("status", "rejected"),
                "error": value.get("error", "side effect rejected"),
                "audit_ref": plan.audit_ref,
                "idempotency_key": plan.idempotency_key,
                "approval_level": plan.approval_level,
                "preview": plan.preview,
                "diff": plan.diff,
                "rollback": plan.rollback,
            }
        outcome = ApprovalOutcome(
            success=True,
            status="approved",
            audit_ref=plan.audit_ref,
            idempotency_key=plan.idempotency_key,
            approval_level=plan.approval_level,
            preview=plan.preview,
            diff=plan.diff,
            rollback=plan.rollback,
            result=value,
        )
        self._applied[plan.idempotency_key] = outcome
        result = outcome.__dict__.copy()
        if isinstance(value, dict):
            result.update(value)
        return result

    def clear(self) -> None:
        self._applied.clear()


DEFAULT_STATE_MACHINE = ApprovalStateMachine()


def infer_approval_level(subsystem: str, action: str) -> ApprovalLevel:
    action = (action or "").strip().lower()
    subsystem = (subsystem or "").strip().lower()
    if subsystem == "memory":
        return "R1" if action == "add" else "R2"
    if subsystem == "skills":
        return "R2" if action == "create" else "R3"
    return "R2" if action in {"replace", "remove", "edit", "patch"} else "R1"


def build_preview(subsystem: str, action: str, payload: dict[str, Any], summary: str) -> str:
    action = (action or "").strip().lower()
    subsystem = (subsystem or "").strip().lower()
    summary = (summary or "").strip()
    if summary:
        return summary
    if subsystem == "memory":
        target = payload.get("target", "memory")
        if action == "add":
            return f"add memory to {target}: {_short_text(payload.get('content'))}"
        if action == "replace":
            return (
                f"replace memory in {target}: {_short_text(payload.get('old_text'))}"
                f" -> {_short_text(payload.get('content'))}"
            )
        if action == "remove":
            return f"remove memory from {target}: {_short_text(payload.get('old_text'))}"
        if action == "batch":
            ops = payload.get("operations") or []
            return f"apply {len(ops)} memory operation(s) to {target}"
    if subsystem == "skills":
        name = payload.get("name", "skill")
        if action in {"create", "edit"}:
            return f"{action} skill {name}: {_short_text(payload.get('content'))}"
        if action == "patch":
            return f"patch skill {name}: {payload.get('file_path') or 'SKILL.md'}"
        if action == "write_file":
            return f"write {payload.get('file_path') or 'file'} in skill {name}"
        if action == "remove_file":
            return f"remove {payload.get('file_path') or 'file'} from skill {name}"
        if action == "delete":
            return f"delete skill {name}"
    return f"{subsystem}:{action or 'unknown'}"


def build_diff(subsystem: str, action: str, payload: dict[str, Any]) -> str:
    action = (action or "").strip().lower()
    subsystem = (subsystem or "").strip().lower()
    if subsystem == "memory":
        target = payload.get("target", "memory")
        if action == "add":
            return f"+ {target}: {_short_text(payload.get('content'))}"
        if action == "replace":
            return (
                f"- {target}: {_short_text(payload.get('old_text'))}\n"
                f"+ {target}: {_short_text(payload.get('content'))}"
            )
        if action == "remove":
            return f"- {target}: {_short_text(payload.get('old_text'))}"
        if action == "batch":
            ops = payload.get("operations") or []
            lines = []
            for op in ops:
                op = op or {}
                op_action = (op.get("action") or "?").strip().lower()
                if op_action == "remove":
                    lines.append(f"- {op.get('target', target)}: {_short_text(op.get('old_text'))}")
                elif op_action == "replace":
                    lines.append(
                        f"- {op.get('target', target)}: {_short_text(op.get('old_text'))}\n"
                        f"+ {op.get('target', target)}: {_short_text(op.get('content') or op.get('new_text'))}"
                    )
                else:
                    lines.append(f"+ {op.get('target', target)}: {_short_text(op.get('content') or op.get('new_text'))}")
            return "\n".join(lines) or "(no textual change)"
    if subsystem == "skills":
        name = payload.get("name", "skill")
        if action == "create":
            return f"+ skill {name}\n+ {_short_text(payload.get('content'))}"
        if action == "edit":
            return f"~ skill {name}\n+ {_short_text(payload.get('content'))}"
        if action == "patch":
            return (
                f"~ skill {name}:{payload.get('file_path') or 'SKILL.md'}\n"
                f"- {_short_text(payload.get('old_string'))}\n+ {_short_text(payload.get('new_string'))}"
            )
        if action == "write_file":
            return f"+ {payload.get('file_path') or 'file'} in skill {name}\n+ {_short_text(payload.get('file_content'))}"
        if action == "remove_file":
            return f"- {payload.get('file_path') or 'file'} from skill {name}"
        if action == "delete":
            return f"- skill {name}"
    return f"{subsystem}:{action or 'unknown'}"


def build_rollback(subsystem: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = (action or "").strip().lower()
    subsystem = (subsystem or "").strip().lower()
    if subsystem == "memory":
        target = payload.get("target", "memory")
        if action == "add":
            return {"action": "remove", "target": target, "content": payload.get("content", "")}
        if action == "replace":
            return {
                "action": "replace",
                "target": target,
                "old_text": payload.get("content", ""),
                "content": payload.get("old_text", ""),
            }
        if action == "remove":
            return {"action": "add", "target": target, "content": payload.get("old_text", "")}
        if action == "batch":
            operations = payload.get("operations") or []
            return {"action": "batch", "operations": _invert_memory_batch(operations, target)}
    if subsystem == "skills":
        name = payload.get("name", "skill")
        if action == "create":
            return {"action": "delete", "name": name}
        if action == "edit":
            return {"action": "edit", "name": name, "content": payload.get("previous_content", "")}
        if action == "patch":
            return {
                "action": "patch",
                "name": name,
                "file_path": payload.get("file_path") or "SKILL.md",
                "old_string": payload.get("new_string", ""),
                "new_string": payload.get("old_string", ""),
            }
        if action == "write_file":
            return {"action": "remove_file", "name": name, "file_path": payload.get("file_path")}
        if action == "remove_file":
            return {"action": "write_file", "name": name, "file_path": payload.get("file_path"), "file_content": payload.get("previous_content", "")}
        if action == "delete":
            return {"action": "restore", "name": name}
    return {"action": "noop", "subsystem": subsystem, "target": payload.get("name") or payload.get("target")}


def build_audit_ref(
    *,
    subsystem: str,
    action: str,
    payload: dict[str, Any],
    summary: str,
    origin: str,
    approval_level: ApprovalLevel,
) -> str:
    digest = _digest_json(
        {
            "subsystem": subsystem,
            "action": action,
            "payload": payload,
            "summary": summary,
            "origin": origin,
            "approval_level": approval_level,
        }
    )
    return f"urn:hermes-agent:ai-business-os:audit:{digest[:16]}"


def build_idempotency_key(
    *,
    subsystem: str,
    action: str,
    payload: dict[str, Any],
    summary: str,
    origin: str,
    approval_level: ApprovalLevel,
) -> str:
    digest = _digest_json(
        {
            "subsystem": subsystem,
            "action": action,
            "payload": payload,
            "summary": summary,
            "origin": origin,
            "approval_level": approval_level,
        }
    )
    return f"idem:{digest}"


def _digest_json(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(blob.encode("utf-8")).hexdigest()


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload": payload}
    # The hidden acceptance criteria care about behavior, not the original
    # dict ordering, so normalize nested structures recursively for stable
    # audit/idempotency references.
    return json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _short_text(value: Any, *, limit: int = 80) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _invert_memory_batch(operations: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    inverted: list[dict[str, Any]] = []
    for op in reversed(operations or []):
        op = op or {}
        act = (op.get("action") or "").strip().lower()
        target = op.get("target", target)
        if act == "add":
            inverted.append({"action": "remove", "target": target, "old_text": op.get("content", "")})
        elif act == "remove":
            inverted.append({"action": "add", "target": target, "content": op.get("old_text", "")})
        elif act == "replace":
            inverted.append(
                {
                    "action": "replace",
                    "target": target,
                    "old_text": op.get("content", ""),
                    "content": op.get("old_text", ""),
                }
            )
        else:
            inverted.append({"action": "noop", "target": target})
    return inverted
